"""Vault configuration loading and transition table construction.

Loads vault config from YAML, validates structure, and builds the lifecycle
transition table used by LifecycleService for state machine validation.
"""

from pathlib import Path

import jsonschema
import yaml
from pydantic import BaseModel, Field, PrivateAttr

from sage.models.schemas import VaultIdStr


class VaultIdentity(BaseModel):
    id: VaultIdStr
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
    metadata_schema: dict | None = None
    """Optional JSON Schema (draft 2020-12) fragment validating tier3_metadata
    payloads for documents of this doc_type. When omitted, this doc_type
    rejects any tier3_metadata payload at the SAGE API boundary."""


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

    _tier3_validators: dict[str, jsonschema.protocols.Validator] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        """Build the tier3 validator cache on every construction.

        Called by Pydantic v2 after ``model_validate`` finishes. Keeps the
        cache symmetric whether the config was constructed via
        ``load_vault_config`` (production path) or ``model_validate``
        directly (test fixtures, vault-management code).
        """
        self.build_tier3_validators()

    def valid_doc_type_values(self) -> set[str]:
        return {dt.value for dt in self.document_types.doc_types}

    def build_tier3_validators(self) -> None:
        """Construct and cache a jsonschema Validator per doc_type that
        declared a metadata_schema. Called via ``model_post_init`` so a
        malformed metadata_schema surfaces at construction time rather
        than at the first ingest call.

        Raises jsonschema.SchemaError if any declared metadata_schema is not
        itself valid JSON Schema. ``load_vault_config`` and
        ``vault_management._validate_config`` catch and wrap the error
        with vault-config context.
        """
        self._tier3_validators.clear()
        for dt in self.document_types.doc_types:
            if dt.metadata_schema is None:
                continue
            # Validate the schema fragment itself before caching; otherwise
            # the SchemaError would not surface until the first validate()
            # call at ingest time.
            jsonschema.Draft202012Validator.check_schema(dt.metadata_schema)
            self._tier3_validators[dt.value] = jsonschema.Draft202012Validator(dt.metadata_schema)

    def tier3_validator(self, doc_type: str) -> jsonschema.protocols.Validator | None:
        """Return the validator for a doc_type, or None if the doc_type has
        no metadata_schema declared. Callers in the ingestion and metadata
        services treat a None return as 'reject any tier3_metadata payload
        for this doc_type' (the strict no-loose-mode decision recorded in
        the T-0004 implementation plan).
        """
        return self._tier3_validators.get(doc_type)


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

    def validate_transition(self, current_state: str, action: str) -> tuple[str, str | None] | None:
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
    """Load and validate vault config from a YAML file.

    The tier3 validator cache is built by ``VaultConfig.model_post_init``
    during ``model_validate``; a malformed ``metadata_schema`` therefore
    surfaces as ``jsonschema.SchemaError`` propagating from
    ``model_validate``. Callers in upper layers (e.g.
    ``sage.vault_management._validate_config``) wrap the error as
    ``VaultConfigValidationError`` for the SAGE API error surface.
    ``sage.config`` cannot import ``sage.api.errors`` directly per the
    repository's import-linter contract (storage / config / adapters
    layer must not depend on the api layer).
    """
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    return VaultConfig.model_validate(raw)


def build_transition_table(config: VaultConfig) -> TransitionTable:
    """Build the transition lookup table from a loaded vault config."""
    return TransitionTable(config.lifecycle.transitions)
