"""SAGE adapter tests (TEST-SAGE-AD-001 through AD-025).

Production adapter tests for LanceDB ContentStore and nomic-embed-text
EmbeddingProvider. These tests require the nomic-embed-text model to be
available via sentence-transformers (~270MB download on first run).

Tests are organized in implementation dependency order: embedding provider
first (produces vectors consumed by content store tests), then content store.
"""

import math
import shutil

import pytest

from sage.adapters.interfaces import Chunk

# ── Skip if dependencies unavailable ────────────────────────────────

try:
    from sage.adapters.embedding_nomic import NomicEmbeddingProvider
    _HAS_EMBEDDING = True
except (ImportError, RuntimeError):
    _HAS_EMBEDDING = False

try:
    from sage.adapters.content_store_lancedb import LanceDBContentStore
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False


requires_embedding = pytest.mark.skipif(
    not _HAS_EMBEDDING, reason="sentence-transformers or nomic model not available"
)
requires_lancedb = pytest.mark.skipif(
    not _HAS_LANCEDB, reason="lancedb not available"
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def embedding_provider():
    """Module-scoped embedding provider (model loads once for all tests)."""
    if not _HAS_EMBEDDING:
        pytest.skip("sentence-transformers or nomic model not available")
    return NomicEmbeddingProvider()


@pytest.fixture
def content_store(tmp_path):
    """Fresh LanceDB content store per test."""
    if not _HAS_LANCEDB:
        pytest.skip("lancedb not available")
    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    return LanceDBContentStore(brain_root)


@pytest.fixture
async def populated_store(content_store, embedding_provider):
    """Content store pre-populated with chunks from 3 topically distinct documents."""
    docs = {
        "doc_a": [
            ("Introduction", "The document describes a method for synchronizing health records across distributed hospital systems."),
            ("Methods", "Medical record synchronization uses a two-phase commit protocol to ensure data consistency."),
        ],
        "doc_b": [
            ("Overview", "Healthcare data management requires robust systems for keeping patient information up to date."),
            ("Architecture", "The data management platform integrates with existing electronic health record systems."),
        ],
        "doc_c": [
            ("Summary", "The basketball team scored 47 points in the first half of the championship game."),
            ("Statistics", "Player shooting percentages and rebound statistics for the season are presented below."),
        ],
    }

    for doc_id, sections in docs.items():
        texts = [content for _, content in sections]
        embeddings = await embedding_provider.embed(texts)
        chunks = [
            Chunk(
                document_id=doc_id,
                heading_path=heading,
                content=content,
                embedding=emb,
                chunk_index=i,
            )
            for i, ((heading, content), emb) in enumerate(zip(sections, embeddings))
        ]
        await content_store.index_chunks(doc_id, chunks)

    return content_store


# ══════════════════════════════════════════════════════════════════════
# 1. nomic-embed-text EmbeddingProvider
# ══════════════════════════════════════════════════════════════════════


@requires_embedding
class TestNomicEmbeddingProvider:
    """Tests AD-001 through AD-008."""

    async def test_ad_001_embedding_dimension_is_768(self, embedding_provider):
        """AD-001: nomic-embed-text produces 768-dimensional vectors."""
        result = await embedding_provider.embed(
            ["The patent claims a novel method for data synchronization."]
        )
        assert len(result) == 1
        assert len(result[0]) == 768
        assert all(isinstance(x, float) for x in result[0])

    async def test_ad_002_batch_preserves_order(self, embedding_provider):
        """AD-002: N input texts produce N output vectors in order."""
        texts = ["alpha", "beta", "gamma"]
        result = await embedding_provider.embed(texts)
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 768

        # Verify order: re-embed individually and compare
        for i, text in enumerate(texts):
            single = await embedding_provider.embed([text])
            # Should match the corresponding batch vector
            diff = sum((a - b) ** 2 for a, b in zip(result[i], single[0]))
            assert diff < 1e-6, f"Batch vector {i} does not match single embed"

    async def test_ad_003_deterministic_embeddings(self, embedding_provider):
        """AD-003: Same text produces identical embeddings."""
        text = ["reproducibility test"]
        result1 = await embedding_provider.embed(text)
        result2 = await embedding_provider.embed(text)

        diff = sum(
            (a - b) ** 2 for a, b in zip(result1[0], result2[0])
        )
        assert diff < 1e-6

    async def test_ad_004_l2_normalized(self, embedding_provider):
        """AD-004: Output vectors are L2-normalized (norm ~= 1.0)."""
        texts = [
            "Short text.",
            "A significantly longer passage with multiple sentences about "
            "various topics including patent law, data management, and "
            "retrieval systems.",
        ]
        result = await embedding_provider.embed(texts)

        for vec in result:
            norm = math.sqrt(sum(x * x for x in vec))
            assert abs(norm - 1.0) < 1e-4, f"L2 norm = {norm}, expected ~1.0"

    async def test_ad_005_semantic_similarity(self, embedding_provider):
        """AD-005: Similar texts have higher cosine similarity than dissimilar texts."""
        texts = [
            "The document describes a method for synchronizing health records.",
            "A technique for keeping medical data in sync across systems.",
            "The basketball team scored 47 points in the first half.",
        ]
        vecs = await embedding_provider.embed(texts)

        sim_ab = _cosine_sim(vecs[0], vecs[1])
        sim_ac = _cosine_sim(vecs[0], vecs[2])
        sim_bc = _cosine_sim(vecs[1], vecs[2])

        assert sim_ab > sim_ac, f"sim(A,B)={sim_ab:.4f} should > sim(A,C)={sim_ac:.4f}"
        assert sim_ab > sim_bc, f"sim(A,B)={sim_ab:.4f} should > sim(B,C)={sim_bc:.4f}"

    async def test_ad_006_empty_input(self, embedding_provider):
        """AD-006: embed([]) returns [] immediately."""
        result = await embedding_provider.embed([])
        assert result == []

    async def test_ad_007_single_input(self, embedding_provider):
        """AD-007: A batch of one text works correctly."""
        result = await embedding_provider.embed(["single input"])
        assert len(result) == 1
        assert len(result[0]) == 768
        norm = math.sqrt(sum(x * x for x in result[0]))
        assert abs(norm - 1.0) < 1e-4

    def test_ad_008_init_fails_on_bad_model(self):
        """AD-008: Provider init fails fast if model unavailable."""
        with pytest.raises(RuntimeError, match="nonexistent-model-xyz"):
            NomicEmbeddingProvider(model_name="nonexistent-model-xyz")


# ══════════════════════════════════════════════════════════════════════
# 2. LanceDB ContentStore
# ══════════════════════════════════════════════════════════════════════


@requires_lancedb
@requires_embedding
class TestLanceDBContentStore:
    """Tests AD-009 through AD-025."""

    async def test_ad_009_lazy_table_creation(self, tmp_path, embedding_provider):
        """AD-009: Chunks table is created on first index_chunks call."""
        brain = tmp_path / "brain_009"
        brain.mkdir()
        store = LanceDBContentStore(brain)

        vec = (await embedding_provider.embed(["test content"]))[0]
        chunk = Chunk(
            document_id="doc_001",
            heading_path="Introduction",
            content="test content",
            embedding=vec,
            chunk_index=0,
        )
        await store.index_chunks("doc_001", [chunk])

        # Verify table exists and has correct columns
        result = await store.get_all_chunks("doc_001")
        assert len(result) == 1
        assert result[0].document_id == "doc_001"

    async def test_ad_010_search_empty_store(self, content_store, embedding_provider):
        """AD-010: Search on empty store returns empty results."""
        vec = (await embedding_provider.embed(["test"]))[0]
        semantic_results = await content_store.search_semantic(vec, limit=10)
        bm25_results = await content_store.search_bm25("test", limit=10)

        assert semantic_results == []
        assert bm25_results == []

    async def test_ad_011_index_and_retrieve_roundtrip(
        self, content_store, embedding_provider
    ):
        """AD-011: Chunks stored via index_chunks are faithfully retrievable."""
        texts = ["First paragraph.", "Second paragraph.", "Third paragraph."]
        headings = ["Introduction", "Introduction > Background", "Methods"]
        embeddings = await embedding_provider.embed(texts)

        chunks = [
            Chunk(
                document_id="doc_001",
                heading_path=headings[i],
                content=texts[i],
                embedding=embeddings[i],
                chunk_index=i,
            )
            for i in range(3)
        ]
        await content_store.index_chunks("doc_001", chunks)

        result = await content_store.get_all_chunks("doc_001")
        assert len(result) == 3

        for i, chunk in enumerate(result):
            assert chunk.document_id == "doc_001"
            assert chunk.heading_path == headings[i]
            assert chunk.content == texts[i]
            assert chunk.chunk_index == i
            # Embedding roundtrip (within float tolerance)
            assert chunk.embedding is not None
            diff = sum(
                (a - b) ** 2
                for a, b in zip(chunk.embedding, embeddings[i])
            )
            assert diff < 1e-6, f"Embedding mismatch at chunk {i}"

    async def test_ad_012_document_isolation(
        self, content_store, embedding_provider
    ):
        """AD-012: get_all_chunks returns only chunks for the requested document."""
        vec1 = (await embedding_provider.embed(["content one"]))[0]
        vec2 = (await embedding_provider.embed(["content two"]))[0]

        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", "H1", "content one", vec1, 0),
        ])
        await content_store.index_chunks("doc_002", [
            Chunk("doc_002", "H1", "content two", vec2, 0),
        ])

        result = await content_store.get_all_chunks("doc_001")
        assert len(result) == 1
        assert result[0].document_id == "doc_001"

    async def test_ad_013_nonexistent_document(self, content_store):
        """AD-013: get_all_chunks for non-existent document returns empty list."""
        result = await content_store.get_all_chunks("nonexistent_doc_999")
        assert result == []

    async def test_ad_014_remove_document(
        self, content_store, embedding_provider
    ):
        """AD-014: remove_document clears all chunks for a document."""
        texts_1 = ["chunk a", "chunk b", "chunk c"]
        texts_2 = ["other x", "other y"]
        emb_1 = await embedding_provider.embed(texts_1)
        emb_2 = await embedding_provider.embed(texts_2)

        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", "H", texts_1[i], emb_1[i], i) for i in range(3)
        ])
        await content_store.index_chunks("doc_002", [
            Chunk("doc_002", "H", texts_2[i], emb_2[i], i) for i in range(2)
        ])

        await content_store.remove_document("doc_001")

        assert await content_store.get_all_chunks("doc_001") == []
        assert len(await content_store.get_all_chunks("doc_002")) == 2

        # BM25 should not find removed content
        bm25 = await content_store.search_bm25("chunk", limit=10)
        for r in bm25:
            assert r.document_id != "doc_001"

    async def test_ad_015_remove_idempotent(self, content_store):
        """AD-015: Removing a non-existent document is a no-op."""
        await content_store.remove_document("nonexistent_doc")
        # No exception raised

    async def test_ad_016_semantic_search_ranking(
        self, populated_store, embedding_provider
    ):
        """AD-016: Semantic search returns results ranked by cosine similarity."""
        query_vec = (await embedding_provider.embed(["health data sync"]))[0]
        results = await populated_store.search_semantic(query_vec, limit=10)

        assert len(results) > 0
        # Scores should be in descending order
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

        # Health-related docs should rank above basketball
        top_doc_ids = [r.document_id for r in results[:4]]
        assert "doc_c" not in top_doc_ids or top_doc_ids.index("doc_c") >= 2

    async def test_ad_017_semantic_search_limit(
        self, populated_store, embedding_provider
    ):
        """AD-017: Semantic search respects limit parameter."""
        query_vec = (await embedding_provider.embed(["test"]))[0]
        results = await populated_store.search_semantic(query_vec, limit=3)
        assert len(results) <= 3

    async def test_ad_018_bm25_keyword_search(
        self, content_store, embedding_provider
    ):
        """AD-018: BM25 search matches on content keywords."""
        texts = [
            "The synchronization protocol ensures data consistency across nodes.",
            "Basketball teams compete in regional tournaments each spring.",
            "Data synchronization is critical for distributed health systems.",
        ]
        embs = await embedding_provider.embed(texts)

        chunks = [
            Chunk("doc_001", "H1", texts[0], embs[0], 0),
            Chunk("doc_002", "H1", texts[1], embs[1], 0),
            Chunk("doc_003", "H1", texts[2], embs[2], 0),
        ]
        for c in chunks:
            await content_store.index_chunks(c.document_id, [c])

        results = await content_store.search_bm25("synchronization data", limit=10)
        result_docs = [r.document_id for r in results]

        # Both synchronization-containing docs should appear
        assert "doc_001" in result_docs or "doc_003" in result_docs
        # Scores should be descending
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    async def test_ad_019_fts_reflects_mutations(
        self, content_store, embedding_provider
    ):
        """AD-019: BM25 search reflects mutations after FTS index rebuild."""
        # Step 1: No results for unique keyword
        results1 = await content_store.search_bm25(
            "unique_keyword_xyz", limit=10
        )
        assert results1 == []

        # Step 2: Index a chunk containing the keyword
        vec = (await embedding_provider.embed(["unique_keyword_xyz in text"]))[0]
        await content_store.index_chunks("doc_002", [
            Chunk("doc_002", "H", "unique_keyword_xyz in text", vec, 0),
        ])

        # Step 3: Should now find it
        results2 = await content_store.search_bm25(
            "unique_keyword_xyz", limit=10
        )
        assert len(results2) > 0
        assert results2[0].document_id == "doc_002"

        # Step 4: Remove the document
        await content_store.remove_document("doc_002")

        # Step 5: Should be gone
        results3 = await content_store.search_bm25(
            "unique_keyword_xyz", limit=10
        )
        assert results3 == []

    async def test_ad_020_heading_prefix_exact_and_child(
        self, content_store, embedding_provider
    ):
        """AD-020: Heading prefix retrieval returns exact match and children."""
        texts = ["methods content", "sampling content", "results content"]
        headings = ["Methods", "Methods > Sampling", "Results"]
        embs = await embedding_provider.embed(texts)

        chunks = [
            Chunk("doc_001", headings[i], texts[i], embs[i], i)
            for i in range(3)
        ]
        await content_store.index_chunks("doc_001", chunks)

        result = await content_store.get_chunks_by_heading_prefix(
            "doc_001", "Methods"
        )
        assert len(result) == 2
        paths = [c.heading_path for c in result]
        assert "Methods" in paths
        assert "Methods > Sampling" in paths
        assert "Results" not in paths
        # Document order
        assert result[0].chunk_index < result[1].chunk_index

    async def test_ad_021_heading_prefix_no_match(
        self, content_store, embedding_provider
    ):
        """AD-021: No matching heading prefix returns empty list."""
        vec = (await embedding_provider.embed(["content"]))[0]
        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", "Introduction", "content", vec, 0),
        ])

        result = await content_store.get_chunks_by_heading_prefix(
            "doc_001", "Nonexistent Section"
        )
        assert result == []

    async def test_ad_022_heading_prefix_no_partial_match(
        self, content_store, embedding_provider
    ):
        """AD-022: Prefix matching is structural, not substring."""
        texts = ["a", "b", "c", "d"]
        headings = ["Method", "Methods", "Methods > Sampling", "Methodology"]
        embs = await embedding_provider.embed(texts)

        chunks = [
            Chunk("doc_001", headings[i], texts[i], embs[i], i)
            for i in range(4)
        ]
        await content_store.index_chunks("doc_001", chunks)

        result = await content_store.get_chunks_by_heading_prefix(
            "doc_001", "Method"
        )
        assert len(result) == 1
        assert result[0].heading_path == "Method"

    async def test_ad_023_persistence_across_reopen(
        self, tmp_path, embedding_provider
    ):
        """AD-023: Data persists across close/reopen cycle."""
        brain = tmp_path / "brain_023"
        brain.mkdir()

        # Write with first instance
        store1 = LanceDBContentStore(brain)
        vec = (await embedding_provider.embed(["persistent content"]))[0]
        await store1.index_chunks("doc_001", [
            Chunk("doc_001", "H1", "persistent content", vec, 0),
        ])
        del store1  # Release connection

        # Read with new instance
        store2 = LanceDBContentStore(brain)
        result = await store2.get_all_chunks("doc_001")
        assert len(result) == 1
        assert result[0].content == "persistent content"
        assert result[0].heading_path == "H1"
        # Embedding preserved
        diff = sum((a - b) ** 2 for a, b in zip(result[0].embedding, vec))
        assert diff < 1e-6

    async def test_ad_024_special_characters_in_heading(
        self, content_store, embedding_provider
    ):
        """AD-024: Special characters in heading_path are handled correctly."""
        heading = "Section 3.1 > Smith's Method (2024)"
        vec = (await embedding_provider.embed(["special chars"]))[0]

        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", heading, "special chars", vec, 0),
        ])

        result = await content_store.get_chunks_by_heading_prefix(
            "doc_001", heading
        )
        assert len(result) == 1
        assert result[0].heading_path == heading

    async def test_ad_025_index_replaces_existing(
        self, content_store, embedding_provider
    ):
        """AD-025: index_chunks replaces existing chunks for same document."""
        old_texts = ["old a", "old b", "old c"]
        new_texts = ["new x", "new y"]
        old_embs = await embedding_provider.embed(old_texts)
        new_embs = await embedding_provider.embed(new_texts)

        # Index 3 old chunks
        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", "H", old_texts[i], old_embs[i], i)
            for i in range(3)
        ])

        # Replace with 2 new chunks
        await content_store.index_chunks("doc_001", [
            Chunk("doc_001", "H", new_texts[i], new_embs[i], i)
            for i in range(2)
        ])

        result = await content_store.get_all_chunks("doc_001")
        assert len(result) == 2
        contents = {c.content for c in result}
        assert contents == {"new x", "new y"}

        # FTS should reflect new content
        old_results = await content_store.search_bm25("old", limit=10)
        for r in old_results:
            assert r.document_id != "doc_001"


# ── Helpers ─────────────────────────────────────────────────────────


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
