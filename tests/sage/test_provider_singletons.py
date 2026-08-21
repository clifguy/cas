"""Process-level singleton factories for nomic and Qwen3 providers.

Verifies that the production embedding and abstraction providers are
constructed once per process, not once per vault. Per-vault construction
caused N model loads at server startup for N discovered vaults, with
N copies of the model resident in memory and N HuggingFace INFO dumps
on the console.

Identity-reuse and divergence-rejection tests for nomic load the real
~270 MB model and are gated by ``@requires_embedding``. Qwen3 ``__init__``
is cheap (config only; MLX loads on first generate_abstract), so its
singleton tests run unconditionally when mlx-lm is importable. The
integration test against ``initialize_services`` clears
``SAGE_TEST_STUB_PROVIDERS`` to force the production-provider branch
(parallels ``test_di_005`` in test_mcp_init.py).

The singleton symbols are imported unconditionally so that the absence
of the implementation surfaces as a collection error (fail-fast), not
as skipped tests.
"""

import pytest

from sage.adapters.abstraction_qwen3 import (
    Qwen3AbstractionProvider,
    _reset_qwen3_singleton,
    get_qwen3_abstraction_provider,
)
from sage.adapters.embedding_nomic import (
    NomicEmbeddingProvider,
    _reset_nomic_singleton,
    get_nomic_embedding_provider,
)
from sage.adapters.stubs import StubEmbeddingProvider
from sage.config import VaultConfig
from tests.sage.conftest import initialize_services_for_test

# ── Capability gates (whether the underlying model packages are available) ──

try:
    import sentence_transformers  # noqa: F401

    _HAS_EMBEDDING = True
except ImportError:
    _HAS_EMBEDDING = False

try:
    import mlx_lm  # noqa: F401

    _HAS_MLX = True
except ImportError:
    _HAS_MLX = False


requires_embedding = pytest.mark.skipif(
    not _HAS_EMBEDDING, reason="sentence-transformers or nomic model not available"
)
requires_mlx = pytest.mark.skipif(
    not _HAS_MLX,
    reason="mlx-lm not available (Apple Silicon only; skipped on Linux CI)",
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_singletons():
    """Clear both singletons before and after every test in this module."""
    _reset_nomic_singleton()
    _reset_qwen3_singleton()
    yield
    _reset_nomic_singleton()
    _reset_qwen3_singleton()


# ══════════════════════════════════════════════════════════════════════
# 1. Nomic embedding provider singleton
# ══════════════════════════════════════════════════════════════════════


@requires_embedding
class TestNomicSingleton:
    """Process-level singleton for NomicEmbeddingProvider."""

    def test_factory_returns_same_instance_on_repeated_calls(self):
        """Two get_nomic_embedding_provider() calls return identical objects.

        Per-vault redundant model loads disappear because every vault's
        initialize_services() resolves to the same cached provider.
        """
        first = get_nomic_embedding_provider()
        second = get_nomic_embedding_provider()
        assert first is second

    def test_factory_rejects_divergent_model_name(self):
        """Requesting a different model_name after the singleton is set raises.

        Silently loading a second model under a different name would defeat
        the singleton; fail fast instead.
        """
        get_nomic_embedding_provider()
        with pytest.raises(RuntimeError, match="nomic"):
            get_nomic_embedding_provider(model_name="some-other-embedding-model")

    def test_reset_allows_fresh_construction(self):
        """After _reset_nomic_singleton(), the next call constructs anew."""
        first = get_nomic_embedding_provider()
        _reset_nomic_singleton()
        second = get_nomic_embedding_provider()
        assert first is not second


# ══════════════════════════════════════════════════════════════════════
# 2. Qwen3 abstraction provider singleton
# ══════════════════════════════════════════════════════════════════════


@requires_mlx
class TestQwen3Singleton:
    """Process-level singleton for Qwen3AbstractionProvider.

    Qwen3 __init__ is cheap (MLX loads lazily on first generate_abstract),
    so these tests do not need a model-availability gate beyond the
    mlx-lm import guard.
    """

    MODEL_ID = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"

    def test_factory_returns_same_instance_on_repeated_calls(self):
        first = get_qwen3_abstraction_provider(model_id=self.MODEL_ID)
        second = get_qwen3_abstraction_provider(model_id=self.MODEL_ID)
        assert first is second
        assert isinstance(first, Qwen3AbstractionProvider)

    def test_factory_rejects_divergent_model_id(self):
        get_qwen3_abstraction_provider(model_id=self.MODEL_ID)
        with pytest.raises(RuntimeError, match="Qwen3|model_id"):
            get_qwen3_abstraction_provider(model_id="some-other-abstraction-model")

    def test_reset_allows_fresh_construction(self):
        first = get_qwen3_abstraction_provider(model_id=self.MODEL_ID)
        _reset_qwen3_singleton()
        second = get_qwen3_abstraction_provider(model_id=self.MODEL_ID)
        assert first is not second


# ══════════════════════════════════════════════════════════════════════
# 3. initialize_services wiring
# ══════════════════════════════════════════════════════════════════════


def _build_vault_config_dict(brain_dir, sources_dir, vault_id: str) -> dict:
    """Build a complete VaultConfig dict pinned to the given brain/sources dirs.

    Mirrors the structure of ``minimal_vault_config_dict`` in conftest.py
    but exposes brain_root and sources_root as parameters so a single test
    can construct multiple vault configs.
    """
    return {
        "vault": {
            "id": vault_id,
            "name": vault_id,
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [{"value": "note", "label": "Note"}],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "metadata_extraction": {},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
    }


@requires_embedding
async def test_initialize_services_shares_embedding_provider_across_vaults(tmp_path, monkeypatch):
    """Two vault inits share the embedding provider via the singleton.

    Regression guard against re-introducing per-vault NomicEmbeddingProvider()
    construction in initialize_services. Clears SAGE_TEST_STUB_PROVIDERS to
    force the production branch (parallels test_di_005 in test_mcp_init.py).
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    brain_a = tmp_path / "vault_a" / "brain"
    sources_a = tmp_path / "vault_a" / "sources"
    brain_a.mkdir(parents=True)
    sources_a.mkdir(parents=True)
    brain_b = tmp_path / "vault_b" / "brain"
    sources_b = tmp_path / "vault_b" / "sources"
    brain_b.mkdir(parents=True)
    sources_b.mkdir(parents=True)

    cfg_a = VaultConfig.model_validate(_build_vault_config_dict(brain_a, sources_a, "vault_a"))
    cfg_b = VaultConfig.model_validate(_build_vault_config_dict(brain_b, sources_b, "vault_b"))

    # Post CAS-ADR-030 the abstraction provider is built once at SAGE stack
    # startup and threaded through; inject an explicit stub to satisfy that
    # contract while keeping the embedding-singleton-sharing assertion the
    # actual focus of the test.
    from sage.adapters.stubs import StubAbstractionProvider

    stub_abstract = StubAbstractionProvider()
    async with (
        initialize_services_for_test(cfg_a, abstraction_provider=stub_abstract) as services_a,
        initialize_services_for_test(cfg_b, abstraction_provider=stub_abstract) as services_b,
    ):
        embed_a = services_a.ingestion_service._embedding
        embed_b = services_b.ingestion_service._embedding
        assert isinstance(embed_a, NomicEmbeddingProvider)
        assert embed_a is embed_b, (
            "Per-vault NomicEmbeddingProvider() construction has returned; "
            "the singleton factory is no longer being routed through"
        )
        # Confirm the same instance is wired into retrieval and utilities too.
        assert services_a.retrieval_service._embedding is embed_a
        assert services_a.utilities_service._embedding is embed_a
        assert services_b.retrieval_service._embedding is embed_a


async def test_initialize_services_explicit_override_bypasses_singleton(tmp_path):
    """Caller-supplied embedding_provider is used as-is, not replaced by the singleton.

    Preserves the existing injection contract used by hermetic test fixtures
    and the SAGE_TEST_STUB_PROVIDERS path. Runs unconditionally because it
    never touches the real model.
    """
    brain = tmp_path / "brain"
    sources = tmp_path / "sources"
    brain.mkdir()
    sources.mkdir()
    cfg = VaultConfig.model_validate(_build_vault_config_dict(brain, sources, "vault_override"))

    stub = StubEmbeddingProvider()
    async with initialize_services_for_test(cfg, embedding_provider=stub) as services:
        assert services.ingestion_service._embedding is stub
        assert services.retrieval_service._embedding is stub
        assert services.utilities_service._embedding is stub
