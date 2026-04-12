"""SAGE-specific test fixtures.

Extends the root conftest.py fixtures with SAGE storage, services,
and stub adapters. Each test gets an isolated temp directory and
fresh SQLite database via pytest's tmp_path fixture.
"""

import pytest
import yaml
from pathlib import Path

from sage.adapters.stubs import (
    FailingAbstractionProvider,
    SeededEmbeddingProvider,
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig, build_transition_table
from sage.models.enums import SourceType
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.user_service import UserService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


@pytest.fixture
def tmp_vault_dir(tmp_path):
    """Create a temporary vault directory structure."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    return tmp_path


@pytest.fixture
def minimal_vault_config_dict(tmp_vault_dir):
    """Minimal vault config dict for testing (base states only)."""
    return {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "note", "label": "Note"},
                {"value": "memo", "label": "Memo"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "superseded", "label": "Superseded"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "superseded",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "superseded", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {"review_required": False},
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


@pytest.fixture
def minimal_config(minimal_vault_config_dict):
    """Parsed VaultConfig from minimal dict."""
    return VaultConfig.model_validate(minimal_vault_config_dict)


@pytest.fixture
def pim_health_vault_config_dict(pim_health_config, tmp_vault_dir):
    """PIM Health config with test-safe paths."""
    import copy
    config = copy.deepcopy(pim_health_config)
    config["vault"]["storage_root"] = str(tmp_vault_dir / "sources")
    config["vault"]["brain_root"] = str(tmp_vault_dir / "brain")
    return config


@pytest.fixture
def pim_health_config_obj(pim_health_vault_config_dict):
    """Parsed VaultConfig from PIM Health dict."""
    return VaultConfig.model_validate(pim_health_vault_config_dict)


@pytest.fixture
async def graph_store(tmp_vault_dir):
    """Initialized graph store in a temp directory."""
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
def lock_manager():
    return DocumentLockManager()


@pytest.fixture
def stub_content_store():
    return StubContentStore()


@pytest.fixture
def stub_embedding_provider():
    return StubEmbeddingProvider()


@pytest.fixture
def stub_abstraction_provider():
    return StubAbstractionProvider()


@pytest.fixture
def failing_abstraction_provider():
    return FailingAbstractionProvider()


@pytest.fixture
def user_service(graph_store, minimal_config):
    return UserService(graph_store, minimal_config)


@pytest.fixture
def lifecycle_service(graph_store, lock_manager, minimal_config):
    return LifecycleService(graph_store, lock_manager, minimal_config)


@pytest.fixture
def pim_lifecycle_service(graph_store, lock_manager, pim_health_config_obj):
    return LifecycleService(graph_store, lock_manager, pim_health_config_obj)


@pytest.fixture
def metadata_service(graph_store, lock_manager, minimal_config, stub_content_store):
    return MetadataService(graph_store, lock_manager, minimal_config, stub_content_store)


@pytest.fixture
def graph_ops_service(graph_store, minimal_config):
    return GraphOpsService(graph_store, minimal_config)


@pytest.fixture
def pim_graph_ops_service(graph_store, pim_health_config_obj):
    return GraphOpsService(graph_store, pim_health_config_obj)


@pytest.fixture
def ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


@pytest.fixture
def ingestion_service_no_abstraction(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Ingestion service with abstraction disabled (BH-025)."""
    config_dict = minimal_vault_config_dict.copy()
    config_dict["abstraction"] = {"enabled": False}
    config = VaultConfig.model_validate(config_dict)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


@pytest.fixture
def ingestion_service_failing_llm(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    failing_abstraction_provider,
    minimal_config,
):
    """Ingestion service with failing LLM (BH-024)."""
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=failing_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )
