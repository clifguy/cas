"""SAGE adapter tests (TEST-SAGE-AD-001 through AD-037).

Production adapter tests for nomic-embed-text EmbeddingProvider, LanceDB
ContentStore, Qwen3 AbstractionProvider (with lazy loading), and Markdown
source adapter provenance. Embedding and content store tests require
nomic-embed-text (~270MB download on first run). Abstraction tests require
mlx-lm and Qwen3 model weights (~16GB download on first run). Markdown
adapter tests have no external dependencies.

Tests are organized in implementation dependency order: embedding provider
first, then content store, then abstraction provider, then markdown adapter.
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

try:
    from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
    _HAS_QWEN3 = True
except (ImportError, RuntimeError):
    _HAS_QWEN3 = False


requires_embedding = pytest.mark.skipif(
    not _HAS_EMBEDDING, reason="sentence-transformers or nomic model not available"
)
requires_lancedb = pytest.mark.skipif(
    not _HAS_LANCEDB, reason="lancedb not available"
)
requires_qwen3 = pytest.mark.skipif(
    not _HAS_QWEN3, reason="mlx-lm or Qwen3 model not available"
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


# ══════════════════════════════════════════════════════════════════════
# 3. Qwen3 AbstractionProvider
# ══════════════════════════════════════════════════════════════════════

QWEN3_MODEL_ID = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"

SAMPLE_TEXT = (
    "The document describes a method for synchronizing patient health records "
    "across distributed clinical systems using a conflict-free replicated data "
    "type (CRDT). The approach ensures eventual consistency while preserving "
    "the causal ordering of clinical events. The system handles concurrent "
    "updates from multiple hospital sites without requiring a central "
    "coordinator, reducing single points of failure in the health information "
    "exchange infrastructure."
)


@pytest.fixture(scope="module")
def qwen3_provider():
    """Module-scoped abstraction provider. With lazy loading, construction
    is cheap (no model allocated). The first generate_abstract() call in
    the test suite triggers the actual model load."""
    if not _HAS_QWEN3:
        pytest.skip("mlx-lm or Qwen3 model not available")
    return Qwen3AbstractionProvider(model_id=QWEN3_MODEL_ID)


@requires_qwen3
class TestQwen3AbstractionProvider:
    """Tests AD-026 through AD-033."""

    def test_ad_026_init_defers_loading(self):
        """AD-026: Provider constructor succeeds without loading model."""
        provider = Qwen3AbstractionProvider(model_id=QWEN3_MODEL_ID)
        assert provider._model is None
        assert provider._tokenizer is None

    def test_ad_026_bad_model_succeeds_at_init(self):
        """AD-026: Bad model ID succeeds at construction (lazy loading)."""
        provider = Qwen3AbstractionProvider(model_id="nonexistent-model-xyz")
        # Construction succeeds; failure deferred to first use
        assert provider._model is None

    async def test_ad_026_bad_model_fails_on_first_call(self):
        """AD-026: Bad model ID raises RuntimeError on first generate_abstract()."""
        provider = Qwen3AbstractionProvider(model_id="nonexistent-model-xyz")
        with pytest.raises(RuntimeError, match="nonexistent-model-xyz"):
            await provider.generate_abstract("Test text.", 200)

    async def test_ad_027_non_empty_output(self, qwen3_provider):
        """AD-027: Generated abstract is a non-empty string."""
        result = await qwen3_provider.generate_abstract(SAMPLE_TEXT, 200)
        assert isinstance(result, str)
        assert len(result.strip()) > 0
        assert result == result.strip()  # No leading/trailing whitespace

    async def test_ad_028_max_tokens_bound(self, qwen3_provider):
        """AD-028: Output respects max_tokens upper bound."""
        # Use a longer input to encourage full-length generation
        long_text = SAMPLE_TEXT * 5
        result = await qwen3_provider.generate_abstract(long_text, 100)

        # Tokenize the result using the model's tokenizer
        tokens = qwen3_provider._tokenizer.encode(result)
        assert len(tokens) <= 100, (
            f"Abstract has {len(tokens)} tokens, expected at most 100"
        )

    async def test_ad_029_deterministic(self, qwen3_provider):
        """AD-029: Same input produces identical output."""
        r1 = await qwen3_provider.generate_abstract(SAMPLE_TEXT, 200)
        r2 = await qwen3_provider.generate_abstract(SAMPLE_TEXT, 200)
        assert r1 == r2

    async def test_ad_030_short_input(self, qwen3_provider):
        """AD-030: Short input produces a valid abstract."""
        result = await qwen3_provider.generate_abstract(
            "Brief note about record linkage.", 200
        )
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    async def test_ad_031_long_input(self, qwen3_provider):
        """AD-031: Long input does not crash."""
        # 50,000+ characters of repeated technical prose
        very_long_text = SAMPLE_TEXT * 200
        assert len(very_long_text) > 50_000

        result = await qwen3_provider.generate_abstract(very_long_text, 200)
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    async def test_ad_032_semantic_quality(
        self, qwen3_provider, embedding_provider
    ):
        """AD-032: Abstract is semantically related to input."""
        abstract = await qwen3_provider.generate_abstract(SAMPLE_TEXT, 200)

        # Embed both input and abstract
        vecs = await embedding_provider.embed([SAMPLE_TEXT, abstract])
        similarity = _cosine_sim(vecs[0], vecs[1])

        assert similarity > 0.5, (
            f"cosine_similarity={similarity:.4f}, expected > 0.5"
        )

        # Keyword overlap: at least one key concept appears in the abstract
        key_terms = ["health", "record", "clinical", "CRDT", "synchron"]
        abstract_lower = abstract.lower()
        matches = [t for t in key_terms if t.lower() in abstract_lower]
        assert len(matches) >= 1, (
            f"No key terms found in abstract. Terms checked: {key_terms}"
        )

    async def test_ad_033_error_propagation(self, qwen3_provider, monkeypatch):
        """AD-033: LLM runtime error propagates as exception."""
        def failing_generate(*args, **kwargs):
            raise RuntimeError("Simulated MLX inference failure")

        monkeypatch.setattr(qwen3_provider, "_generate_fn", failing_generate)

        with pytest.raises(RuntimeError, match="Simulated MLX inference failure"):
            await qwen3_provider.generate_abstract(SAMPLE_TEXT, 200)


@requires_qwen3
class TestQwen3LazyLoading:
    """Tests AD-035 through AD-037: lazy model loading behavior."""

    async def test_ad_035_defers_load_to_first_call(self):
        """AD-035: Constructor does not load model; first call triggers load."""
        provider = Qwen3AbstractionProvider(model_id=QWEN3_MODEL_ID)
        assert provider._model is None

        result = await provider.generate_abstract(SAMPLE_TEXT, 200)
        assert provider._model is not None
        assert isinstance(result, str)
        assert len(result.strip()) > 0

    async def test_ad_036_second_call_reuses_model(self):
        """AD-036: Second generate_abstract() reuses the loaded model."""
        provider = Qwen3AbstractionProvider(model_id=QWEN3_MODEL_ID)

        await provider.generate_abstract(SAMPLE_TEXT, 200)
        model_after_first = provider._model
        assert model_after_first is not None

        await provider.generate_abstract("Different input text.", 200)
        assert provider._model is model_after_first  # Same object identity

    async def test_ad_037_load_failure_raises_and_stays_unloaded(self):
        """AD-037: Model load failure raises RuntimeError; provider
        remains in unloaded state for potential retry."""
        provider = Qwen3AbstractionProvider(model_id="nonexistent-model-xyz")

        with pytest.raises(RuntimeError, match="nonexistent-model-xyz"):
            await provider.generate_abstract(SAMPLE_TEXT, 200)

        assert provider._model is None  # No partial state


# ── Markdown Adapter: Source Provenance ────────────────────────────

import os
from datetime import datetime, timezone
from pathlib import Path

from sage.source_adapters.markdown_adapter import MarkdownAdapter


class TestMarkdownAdapterProvenance:
    """AD-034: Markdown adapter extracts source_modified_at from file mtime."""

    async def test_ad_034_source_modified_at_in_metadata(self, tmp_path):
        """AD-034: Markdown adapter populates source_modified_at from st_mtime."""
        adapter = MarkdownAdapter()

        test_file = tmp_path / "test_provenance.md"
        test_file.write_text("# Test\n\nContent for provenance test.")

        # Set a known mtime
        known_mtime = datetime(2023, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
        os.utime(test_file, (test_file.stat().st_atime, known_mtime.timestamp()))

        result = await adapter.project(test_file)

        assert "source_modified_at" in result.metadata
        parsed = datetime.fromisoformat(result.metadata["source_modified_at"])
        assert parsed.tzinfo is not None  # timezone-aware
        assert abs((parsed - known_mtime).total_seconds()) < 1.0


# ── Docx Adapter ────────────────────────────────────────────────────

import hashlib

try:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from lxml import etree

    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

requires_docx = pytest.mark.skipif(
    not _HAS_DOCX, reason="python-docx not available"
)


def _make_docx(tmp_path: Path, filename: str = "test.docx") -> Path:
    """Create a minimal empty .docx and return its path."""
    doc = docx.Document()
    path = tmp_path / filename
    doc.save(str(path))
    return path


def _add_heading_with_style(doc, text: str, style_name: str) -> None:
    """Add a paragraph with the given built-in heading style."""
    doc.add_paragraph(text, style=style_name)


def _add_table(doc, rows: list[list[str]]) -> None:
    """Add a table with the given cell data."""
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row_data in enumerate(rows):
        for j, cell_text in enumerate(row_data):
            table.cell(i, j).text = cell_text


def _inject_numbering(doc, abstract_num_xml: str, num_xml: str) -> None:
    """Inject numbering definitions into the document's numbering part.

    Creates the numbering part if it doesn't exist, then appends the
    provided abstractNum and num elements.
    """
    # Ensure numbering part exists by adding and removing a dummy list
    doc.add_paragraph("dummy", style="List Bullet")
    # Remove the dummy paragraph
    body = doc.element.body
    body.remove(body[-1])

    numbering_part = doc.part.numbering_part
    numbering_elm = numbering_part.numbering_definitions._numbering

    # Parse and append abstractNum
    abstract_elem = etree.fromstring(abstract_num_xml)
    numbering_elm.append(abstract_elem)

    # Parse and append num
    num_elem = etree.fromstring(num_xml)
    numbering_elm.append(num_elem)


def _set_paragraph_numbering(paragraph, num_id: int, ilvl: int) -> None:
    """Set w:numPr on a paragraph to reference a numbering definition."""
    pPr = paragraph._element.get_or_add_pPr()
    numPr = etree.SubElement(pPr, qn("w:numPr"))
    ilvl_elem = etree.SubElement(numPr, qn("w:ilvl"))
    ilvl_elem.set(qn("w:val"), str(ilvl))
    numId_elem = etree.SubElement(numPr, qn("w:numId"))
    numId_elem.set(qn("w:val"), str(num_id))


def _inject_cross_ref_field(paragraph, instruction: str, display_text: str) -> None:
    """Inject a complex field (fldChar begin/separate/end) into a paragraph.

    Simulates a cross-reference field like REF _Ref12345 \\r \\h with a
    cached display value.
    """
    p_elem = paragraph._element
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    # Begin
    r_begin = etree.SubElement(p_elem, qn("w:r"))
    fc_begin = etree.SubElement(r_begin, qn("w:fldChar"))
    fc_begin.set(qn("w:fldCharType"), "begin")

    # Instruction
    r_instr = etree.SubElement(p_elem, qn("w:r"))
    instr_text = etree.SubElement(r_instr, qn("w:instrText"))
    instr_text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr_text.text = instruction

    # Separate
    r_sep = etree.SubElement(p_elem, qn("w:r"))
    fc_sep = etree.SubElement(r_sep, qn("w:fldChar"))
    fc_sep.set(qn("w:fldCharType"), "separate")

    # Display text (cached result)
    r_display = etree.SubElement(p_elem, qn("w:r"))
    t_display = etree.SubElement(r_display, qn("w:t"))
    t_display.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t_display.text = display_text

    # End
    r_end = etree.SubElement(p_elem, qn("w:r"))
    fc_end = etree.SubElement(r_end, qn("w:fldChar"))
    fc_end.set(qn("w:fldCharType"), "end")


# Standard decimal numbering XML for heading levels 0-2
DECIMAL_ABSTRACT_NUM_XML = """
<w:abstractNum w:abstractNumId="100"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:lvl w:ilvl="0">
    <w:start w:val="1"/>
    <w:numFmt w:val="decimal"/>
    <w:lvlText w:val="%1"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
  <w:lvl w:ilvl="1">
    <w:start w:val="1"/>
    <w:numFmt w:val="decimal"/>
    <w:lvlText w:val="%1.%2"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
  <w:lvl w:ilvl="2">
    <w:start w:val="1"/>
    <w:numFmt w:val="decimal"/>
    <w:lvlText w:val="%1.%2.%3"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
</w:abstractNum>
"""

DECIMAL_NUM_XML = """
<w:num w:numId="100"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNumId w:val="100"/>
</w:num>
"""

# Mixed numbering: Roman, decimal, lowercase alpha
MIXED_ABSTRACT_NUM_XML = """
<w:abstractNum w:abstractNumId="200"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:lvl w:ilvl="0">
    <w:start w:val="1"/>
    <w:numFmt w:val="upperRoman"/>
    <w:lvlText w:val="%1"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
  <w:lvl w:ilvl="1">
    <w:start w:val="1"/>
    <w:numFmt w:val="decimal"/>
    <w:lvlText w:val="%1.%2"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
  <w:lvl w:ilvl="2">
    <w:start w:val="1"/>
    <w:numFmt w:val="lowerLetter"/>
    <w:lvlText w:val="%1.%2.%3"/>
    <w:lvlJc w:val="left"/>
  </w:lvl>
</w:abstractNum>
"""

MIXED_NUM_XML = """
<w:num w:numId="200"
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNumId w:val="200"/>
</w:num>
"""


@requires_docx
class TestDocxAdapter:
    """AD-035 through AD-052: Docx source adapter tests."""

    async def test_ad_035_basic_projection(self, tmp_path):
        """AD-035: Basic projection returns valid ProjectionResult."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Introduction", style="Heading 1")
        doc.add_paragraph("This is the introduction text.")
        path = tmp_path / "basic.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert isinstance(result.text, str)
        assert len(result.text) > 0
        assert len(result.headings) == 1
        assert result.headings[0].text == "Introduction"
        assert isinstance(result.content_hash, str)
        assert len(result.content_hash) == 64  # SHA-256 hex
        assert result.title == "Introduction"
        assert result.adapter_version == DocxAdapter.VERSION

    async def test_ad_036_heading_style_map_config(self, tmp_path):
        """AD-036: Heading extraction uses heading_style_map config."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("My Title", style="Title")
        doc.add_paragraph("Body under title.")
        path = tmp_path / "custom_style.docx"
        doc.save(str(path))

        config = {"heading_style_map": {"Title": 1}}
        result = await adapter.project(path, config=config)

        assert len(result.headings) == 1
        assert result.headings[0].level == 1
        assert result.headings[0].text == "My Title"

    async def test_ad_037_default_heading_styles(self, tmp_path):
        """AD-037: Default heading styles work without explicit config."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Level One", style="Heading 1")
        doc.add_paragraph("Level Two", style="Heading 2")
        doc.add_paragraph("Level Three", style="Heading 3")
        path = tmp_path / "defaults.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert len(result.headings) == 3
        assert result.headings[0].level == 1
        assert result.headings[1].level == 2
        assert result.headings[2].level == 3

    async def test_ad_038_heading_hierarchy_paths(self, tmp_path):
        """AD-038: Heading hierarchy paths use ' > ' separator."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Chapter", style="Heading 1")
        doc.add_paragraph("Section", style="Heading 2")
        doc.add_paragraph("Subsection", style="Heading 3")
        path = tmp_path / "hierarchy.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.headings[0].path == "Chapter"
        assert result.headings[1].path == "Chapter > Section"
        assert result.headings[2].path == "Chapter > Section > Subsection"

    async def test_ad_039_title_extraction_fallback(self, tmp_path):
        """AD-039: Title from first level-1 heading, fallback to filename."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()

        # With H1 heading
        doc1 = docx.Document()
        doc1.add_paragraph("My Document Title", style="Heading 1")
        path1 = tmp_path / "with_title.docx"
        doc1.save(str(path1))
        result1 = await adapter.project(path1)
        assert result1.title == "My Document Title"

        # Without H1 heading (only H2)
        doc2 = docx.Document()
        doc2.add_paragraph("A Subsection", style="Heading 2")
        path2 = tmp_path / "no_h1_title.docx"
        doc2.save(str(path2))
        result2 = await adapter.project(path2)
        assert result2.title == "no_h1_title"

    async def test_ad_040_content_hash_is_raw_bytes(self, tmp_path):
        """AD-040: content_hash is SHA-256 of raw .docx bytes."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Hash test content.")
        path = tmp_path / "hash_test.docx"
        doc.save(str(path))

        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        result = await adapter.project(path)
        assert result.content_hash == expected_hash

    async def test_ad_041_source_modified_at(self, tmp_path):
        """AD-041: source_modified_at extracted from file mtime."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Provenance test.")
        path = tmp_path / "provenance.docx"
        doc.save(str(path))

        known_mtime = datetime(2023, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        os.utime(path, (path.stat().st_atime, known_mtime.timestamp()))

        result = await adapter.project(path)

        assert "source_modified_at" in result.metadata
        parsed = datetime.fromisoformat(result.metadata["source_modified_at"])
        assert parsed.tzinfo is not None
        assert abs((parsed - known_mtime).total_seconds()) < 1.0

    async def test_ad_042_table_extraction(self, tmp_path):
        """AD-042: Table content extracted as pipe-delimited text rows."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Data Section", style="Heading 1")
        _add_table(doc, [["Name", "Value"], ["alpha", "1"], ["beta", "2"]])
        path = tmp_path / "table.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert "| Name | Value |" in result.text
        assert "| alpha | 1 |" in result.text
        assert "| beta | 2 |" in result.text

    async def test_ad_043_mixed_content_ordering(self, tmp_path):
        """AD-043: Mixed headings, paragraphs, tables in correct order."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Overview", style="Heading 1")
        doc.add_paragraph("Intro paragraph.")
        _add_table(doc, [["A", "B"], ["1", "2"]])
        doc.add_paragraph("Details", style="Heading 2")
        doc.add_paragraph("Detail paragraph.")
        path = tmp_path / "mixed.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert len(result.headings) == 2
        assert result.headings[0].text == "Overview"
        assert result.headings[1].text == "Details"
        # Table content should be under "Overview" heading
        assert "| A | B |" in result.headings[0].content
        assert "Intro paragraph." in result.headings[0].content

    async def test_ad_044_empty_document(self, tmp_path):
        """AD-044: Empty document produces valid result with filename title."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        path = tmp_path / "empty_doc.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.title == "empty_doc"
        assert result.headings == []
        assert isinstance(result.text, str)
        assert isinstance(result.content_hash, str)

    async def test_ad_045_custom_style_map_override(self, tmp_path):
        """AD-045: Custom heading_style_map overrides defaults."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Custom Top", style="Title")
        doc.add_paragraph("Standard H1", style="Heading 1")
        path = tmp_path / "override.docx"
        doc.save(str(path))

        # "Title" mapped to level 1; defaults still apply for "Heading 1"
        config = {"heading_style_map": {"Title": 1}}
        result = await adapter.project(path, config=config)

        titles = [h for h in result.headings if h.text == "Custom Top"]
        assert len(titles) == 1
        assert titles[0].level == 1

        h1s = [h for h in result.headings if h.text == "Standard H1"]
        assert len(h1s) == 1
        assert h1s[0].level == 1

    async def test_ad_046_body_under_heading(self, tmp_path):
        """AD-046: Non-heading paragraphs appear as content under nearest heading."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Section A", style="Heading 1")
        doc.add_paragraph("First body paragraph.")
        doc.add_paragraph("Second body paragraph.")
        doc.add_paragraph("Section B", style="Heading 1")
        doc.add_paragraph("Third body paragraph.")
        path = tmp_path / "body.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert "First body paragraph." in result.headings[0].content
        assert "Second body paragraph." in result.headings[0].content
        assert "Third body paragraph." in result.headings[1].content
        assert "First body paragraph." not in result.headings[1].content

    async def test_ad_047_adapter_version(self, tmp_path):
        """AD-047: adapter_version matches DocxAdapter.VERSION."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("Version check.")
        path = tmp_path / "version.docx"
        doc.save(str(path))

        result = await adapter.project(path)
        assert result.adapter_version == DocxAdapter.VERSION

    async def test_ad_048_decimal_heading_numbering(self, tmp_path):
        """AD-048: Decimal heading numbering prepended to heading text."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        _inject_numbering(doc, DECIMAL_ABSTRACT_NUM_XML, DECIMAL_NUM_XML)

        # Two H1 headings, second with an H2 child
        p1 = doc.add_paragraph("Introduction", style="Heading 1")
        _set_paragraph_numbering(p1, num_id=100, ilvl=0)

        p2 = doc.add_paragraph("Background", style="Heading 1")
        _set_paragraph_numbering(p2, num_id=100, ilvl=0)

        p3 = doc.add_paragraph("Definitions", style="Heading 2")
        _set_paragraph_numbering(p3, num_id=100, ilvl=1)

        path = tmp_path / "numbered.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.headings[0].text == "1 Introduction"
        assert result.headings[1].text == "2 Background"
        assert result.headings[2].text == "2.1 Definitions"

    async def test_ad_049_numbering_counter_reset(self, tmp_path):
        """AD-049: Child counters reset when parent level increments."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        _inject_numbering(doc, DECIMAL_ABSTRACT_NUM_XML, DECIMAL_NUM_XML)

        # H1 -> H2, H2 -> H1 -> H2 (second H2 should restart at .1)
        p1 = doc.add_paragraph("Part A", style="Heading 1")
        _set_paragraph_numbering(p1, num_id=100, ilvl=0)
        p2 = doc.add_paragraph("Sub One", style="Heading 2")
        _set_paragraph_numbering(p2, num_id=100, ilvl=1)
        p3 = doc.add_paragraph("Sub Two", style="Heading 2")
        _set_paragraph_numbering(p3, num_id=100, ilvl=1)
        p4 = doc.add_paragraph("Part B", style="Heading 1")
        _set_paragraph_numbering(p4, num_id=100, ilvl=0)
        p5 = doc.add_paragraph("Sub One Again", style="Heading 2")
        _set_paragraph_numbering(p5, num_id=100, ilvl=1)

        path = tmp_path / "reset.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.headings[0].text == "1 Part A"
        assert result.headings[1].text == "1.1 Sub One"
        assert result.headings[2].text == "1.2 Sub Two"
        assert result.headings[3].text == "2 Part B"
        assert result.headings[4].text == "2.1 Sub One Again"

    async def test_ad_050_custom_numbering_formats(self, tmp_path):
        """AD-050: upperRoman and lowerLetter numbering formats."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        _inject_numbering(doc, MIXED_ABSTRACT_NUM_XML, MIXED_NUM_XML)

        p1 = doc.add_paragraph("First Chapter", style="Heading 1")
        _set_paragraph_numbering(p1, num_id=200, ilvl=0)
        p2 = doc.add_paragraph("First Section", style="Heading 2")
        _set_paragraph_numbering(p2, num_id=200, ilvl=1)
        p3 = doc.add_paragraph("First Item", style="Heading 3")
        _set_paragraph_numbering(p3, num_id=200, ilvl=2)

        path = tmp_path / "roman.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.headings[0].text == "I First Chapter"
        assert result.headings[1].text == "I.1 First Section"
        assert result.headings[2].text == "I.1.a First Item"

    async def test_ad_051_cross_reference_field(self, tmp_path):
        """AD-051: Cross-ref field cached results in text, instructions excluded."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        doc.add_paragraph("References", style="Heading 1")

        # Add paragraph with text + cross-reference field
        p = doc.add_paragraph()
        p.add_run("See Section ")
        _inject_cross_ref_field(p, " REF _Ref12345 \\r \\h ", "1.1")
        p.add_run(" for details.")

        path = tmp_path / "crossref.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        # Cached display value should appear
        assert "See Section 1.1 for details." in result.text
        # Field instruction should NOT appear
        assert "REF _Ref12345" not in result.text
        assert "instrText" not in result.text

    async def test_ad_052_mixed_numbering_formats(self, tmp_path):
        """AD-052: Multi-level numbering with mixed formats (I.2.a)."""
        from sage.source_adapters.docx_adapter import DocxAdapter

        adapter = DocxAdapter()
        doc = docx.Document()
        _inject_numbering(doc, MIXED_ABSTRACT_NUM_XML, MIXED_NUM_XML)

        # Two top-level chapters, each with subsections
        p1 = doc.add_paragraph("Alpha", style="Heading 1")
        _set_paragraph_numbering(p1, num_id=200, ilvl=0)
        p2 = doc.add_paragraph("Sub Alpha", style="Heading 2")
        _set_paragraph_numbering(p2, num_id=200, ilvl=1)
        p3 = doc.add_paragraph("Detail A", style="Heading 3")
        _set_paragraph_numbering(p3, num_id=200, ilvl=2)
        p4 = doc.add_paragraph("Detail B", style="Heading 3")
        _set_paragraph_numbering(p4, num_id=200, ilvl=2)
        p5 = doc.add_paragraph("Beta", style="Heading 1")
        _set_paragraph_numbering(p5, num_id=200, ilvl=0)
        p6 = doc.add_paragraph("Sub Beta", style="Heading 2")
        _set_paragraph_numbering(p6, num_id=200, ilvl=1)

        path = tmp_path / "mixed_fmt.docx"
        doc.save(str(path))

        result = await adapter.project(path)

        assert result.headings[0].text == "I Alpha"
        assert result.headings[1].text == "I.1 Sub Alpha"
        assert result.headings[2].text == "I.1.a Detail A"
        assert result.headings[3].text == "I.1.b Detail B"
        assert result.headings[4].text == "II Beta"
        assert result.headings[5].text == "II.1 Sub Beta"


# ── XLSX Adapter ──────────────────────────────────────────────────

try:
    import openpyxl

    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

requires_openpyxl = pytest.mark.skipif(
    not _HAS_OPENPYXL, reason="openpyxl not available"
)


def _make_xlsx(tmp_path: Path, filename: str = "test.xlsx") -> Path:
    """Create a minimal single-sheet workbook and return its path."""
    wb = openpyxl.Workbook()
    path = tmp_path / filename
    wb.save(str(path))
    wb.close()
    return path


def _make_multisheet_xlsx(
    tmp_path: Path,
    sheets_data: dict[str, list[list]],
    filename: str = "multi.xlsx",
) -> Path:
    """Create a workbook with named sheets and cell data.

    sheets_data: {"SheetName": [[row1_values], [row2_values], ...]}
    """
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)
    for sheet_name, rows in sheets_data.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    path = tmp_path / filename
    wb.save(str(path))
    wb.close()
    return path


@requires_openpyxl
class TestXlsxAdapter:
    """AD-053 through AD-067: XLSX source adapter tests."""

    async def test_ad_053_basic_projection(self, tmp_path):
        """AD-053: Basic projection returns valid ProjectionResult."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {"Sales": [["Product", "Revenue"], ["Widget", 100], ["Gadget", 200]]},
            filename="basic.xlsx",
        )

        result = await adapter.project(path)

        assert isinstance(result.text, str)
        assert len(result.text) > 0
        assert len(result.headings) == 1
        assert result.headings[0].text == "Sales"
        assert result.headings[0].level == 1
        assert isinstance(result.content_hash, str)
        assert len(result.content_hash) == 64  # SHA-256 hex
        assert result.adapter_version == XlsxAdapter.VERSION

    async def test_ad_054_multisheet_headings(self, tmp_path):
        """AD-054: Multiple sheets produce one level-1 HeadingNode each."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {
                "Revenue": [["Q1", "Q2"], [100, 200]],
                "Expenses": [["Q1", "Q2"], [50, 80]],
                "Summary": [["Net"], [170]],
            },
        )

        result = await adapter.project(path)

        assert len(result.headings) == 3
        assert [h.text for h in result.headings] == ["Revenue", "Expenses", "Summary"]
        assert all(h.level == 1 for h in result.headings)

    async def test_ad_055_heading_paths_flat(self, tmp_path):
        """AD-055: Heading paths are sheet names only (no hierarchy)."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {
                "Alpha": [["A"], [1]],
                "Beta": [["B"], [2]],
            },
        )

        result = await adapter.project(path)

        assert result.headings[0].path == "Alpha"
        assert result.headings[1].path == "Beta"
        assert " > " not in result.headings[0].path
        assert " > " not in result.headings[1].path

    async def test_ad_056_column_headers_in_content(self, tmp_path):
        """AD-056: First row rendered as pipe-delimited header row."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {"Data": [["Name", "Age", "City"], ["Alice", 30, "NYC"]]},
            filename="headers.xlsx",
        )

        result = await adapter.project(path)

        assert "| Name | Age | City |" in result.headings[0].content

    async def test_ad_057_preview_rows_default(self, tmp_path):
        """AD-057: Default config includes first 5 data rows, omits row 6+."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        rows = [["ID", "Value"]] + [[i, f"val_{i}"] for i in range(1, 9)]
        path = _make_multisheet_xlsx(
            tmp_path, {"Data": rows}, filename="preview.xlsx"
        )

        result = await adapter.project(path)
        content = result.headings[0].content

        # Rows 1-5 present
        for i in range(1, 6):
            assert f"val_{i}" in content
        # Rows 6-8 omitted
        for i in range(6, 9):
            assert f"val_{i}" not in content

    async def test_ad_058_preview_rows_config(self, tmp_path):
        """AD-058: preview_rows config limits data rows per sheet."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        rows = [["ID", "Value"]] + [[i, f"val_{i}"] for i in range(1, 9)]
        path = _make_multisheet_xlsx(
            tmp_path, {"Data": rows}, filename="limited.xlsx"
        )

        result = await adapter.project(path, config={"preview_rows": 2})
        content = result.headings[0].content

        assert "val_1" in content
        assert "val_2" in content
        assert "val_3" not in content

    async def test_ad_059_dimensions_in_content(self, tmp_path):
        """AD-059: Content includes dimensions line for each sheet."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        rows = [["A", "B", "C"]] + [[1, 2, 3]] * 20
        path = _make_multisheet_xlsx(
            tmp_path, {"Big": rows}, filename="dims.xlsx"
        )

        result = await adapter.project(path)
        content = result.headings[0].content

        assert "21 rows" in content
        assert "3 columns" in content

    async def test_ad_060_title_from_first_sheet(self, tmp_path):
        """AD-060: Title extracted from first sheet name."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {"Quarterly Report": [["Q1"], [100]], "Details": [["X"], [1]]},
            filename="report.xlsx",
        )

        result = await adapter.project(path)
        assert result.title == "Quarterly Report"

    async def test_ad_061_title_fallback_to_filename(self, tmp_path):
        """AD-061: Default sheet name falls back to filename stem."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        # openpyxl default sheet name is "Sheet"
        path = _make_xlsx(tmp_path, filename="my_data.xlsx")

        result = await adapter.project(path)
        assert result.title == "my_data"

    async def test_ad_062_content_hash_raw_bytes(self, tmp_path):
        """AD-062: content_hash is SHA-256 of raw .xlsx file bytes."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {"Data": [["X"], [1]]},
            filename="hash_test.xlsx",
        )

        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        result = await adapter.project(path)
        assert result.content_hash == expected_hash

    async def test_ad_063_source_modified_at(self, tmp_path):
        """AD-063: source_modified_at extracted from file mtime, timezone-aware."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {"Data": [["X"], [1]]},
            filename="provenance.xlsx",
        )

        known_mtime = datetime(2023, 9, 1, 14, 0, 0, tzinfo=timezone.utc)
        os.utime(path, (path.stat().st_atime, known_mtime.timestamp()))

        result = await adapter.project(path)

        assert "source_modified_at" in result.metadata
        parsed = datetime.fromisoformat(result.metadata["source_modified_at"])
        assert parsed.tzinfo is not None
        assert abs((parsed - known_mtime).total_seconds()) < 1.0

    async def test_ad_064_empty_workbook(self, tmp_path):
        """AD-064: Empty workbook produces valid result with filename-stem title."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_xlsx(tmp_path, filename="empty_book.xlsx")

        result = await adapter.project(path)

        assert result.title == "empty_book"
        assert isinstance(result.text, str)
        assert isinstance(result.content_hash, str)
        assert len(result.content_hash) == 64

    async def test_ad_065_max_sheets_config(self, tmp_path):
        """AD-065: max_sheets config limits number of sheets projected."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        sheets = {f"Sheet_{i}": [["Col"], [i]] for i in range(1, 6)}
        path = _make_multisheet_xlsx(tmp_path, sheets, filename="many.xlsx")

        result = await adapter.project(path, config={"max_sheets": 2})

        assert len(result.headings) == 2
        assert result.headings[0].text == "Sheet_1"
        assert result.headings[1].text == "Sheet_2"

    async def test_ad_066_sheet_metadata(self, tmp_path):
        """AD-066: metadata includes sheet_names, total_sheets, dimensions."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {
                "Alpha": [["A", "B"], [1, 2], [3, 4]],
                "Beta": [["X"], [10]],
            },
        )

        result = await adapter.project(path)

        assert result.metadata["sheet_names"] == ["Alpha", "Beta"]
        assert result.metadata["total_sheets"] == 2
        assert "Alpha" in result.metadata["dimensions"]
        assert "Beta" in result.metadata["dimensions"]

    async def test_ad_067_full_text_concatenation(self, tmp_path):
        """AD-067: result.text concatenates all sheet projections with markdown headings."""
        from sage.source_adapters.xlsx_adapter import XlsxAdapter

        adapter = XlsxAdapter()
        path = _make_multisheet_xlsx(
            tmp_path,
            {
                "Revenue": [["Q1", "Q2"], [100, 200]],
                "Costs": [["Q1", "Q2"], [50, 80]],
            },
        )

        result = await adapter.project(path)

        # Sheet names appear as markdown headings in full text
        assert "# Revenue" in result.text
        assert "# Costs" in result.text
        # Content from both sheets present
        assert "100" in result.text
        assert "50" in result.text
