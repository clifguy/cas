"""Vault configuration loading and transition table construction.

Loads vault config from YAML, validates structure, and builds the lifecycle
transition table used by LifecycleService for state machine validation.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class VaultIdentity(BaseModel):
    id: str
    name: str
    description: str | None = None
    owner: str
    storage_root: str
    brain_root: str
    visibility: str
    members: list[dict] | None = None
    timezone: str = "UTC"


class LifecycleTransition(BaseModel):
    from_state: str
    action: str
    to_state: str
    semantics: str | None = None
    creates_edge: str | None = None


class LifecycleState(BaseModel):
    value: str
    label: str
    description: str | None = None
    is_terminal: bool = False


class LifecycleConfig(BaseModel):
    states: list[LifecycleState]
    transitions: list[LifecycleTransition]
    base_states_required: bool = True


class AbstractionConfig(BaseModel):
    enabled: bool = True
    model: str | None = None
    max_abstract_tokens: int = 1500
    base_abstract_tokens: int = 150
    tokens_per_word: float = 0.02


class RetrievalHealthConfig(BaseModel):
    assertions_file: str | None = None


class DocTypeEntry(BaseModel):
    value: str
    label: str
    description: str | None = None
    source_types: list[str] | None = None


class DocumentTypesConfig(BaseModel):
    doc_types: list[DocTypeEntry]


class VaultConfig(BaseModel):
    """Root configuration for a SAGE vault."""

    vault: VaultIdentity
    document_types: DocumentTypesConfig
    lifecycle: LifecycleConfig
    source_adapters: dict  # pass-through; validated by JSON Schema
    metadata_extraction: dict  # pass-through
    edge_inference: dict  # pass-through
    access_control_defaults: dict | None = None
    abstraction: AbstractionConfig = Field(default_factory=AbstractionConfig)
    retrieval_health: RetrievalHealthConfig | None = None

    def valid_doc_type_values(self) -> set[str]:
        return {dt.value for dt in self.document_types.doc_types}


class TransitionTable:
    """Lookup structure for lifecycle state machine validation.

    Built from the vault's lifecycle transition list. Queryable by
    current_state to get valid actions, or by (current_state, action)
    to get the target state.
    """

    def __init__(self, transitions: list[LifecycleTransition]) -> None:
        # {from_state: [(action, to_state, creates_edge), ...]}
        self._table: dict[str, list[LifecycleTransition]] = {}
        self._all_actions: set[str] = set()
        for t in transitions:
            if t.from_state == "(new)":
                continue  # ingestion transition, not user-invocable
            self._table.setdefault(t.from_state, []).append(t)
            self._all_actions.add(t.action)

    def validate_transition(
        self, current_state: str, action: str
    ) -> tuple[str, str | None] | None:
        """Return (to_state, creates_edge) if valid, None if invalid."""
        for t in self._table.get(current_state, []):
            if t.action == action:
                return (t.to_state, t.creates_edge)
        return None

    def get_valid_actions(self, current_state: str) -> list[str]:
        """Return action names valid from current_state (BH-012, BH-013)."""
        return [t.action for t in self._table.get(current_state, [])]

    def is_known_action(self, action: str) -> bool:
        """True if action appears anywhere in the transition table.

        Unknown actions get 400; known-but-invalid-from-state get 409.
        """
        return action in self._all_actions


def load_vault_config(config_path: Path) -> VaultConfig:
    """Load and validate vault config from a YAML file."""
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return VaultConfig.model_validate(raw)


def build_transition_table(config: VaultConfig) -> TransitionTable:
    """Build the transition lookup table from a loaded vault config."""
    return TransitionTable(config.lifecycle.transitions)
