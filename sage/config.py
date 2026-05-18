"""Vault configuration loading and transition table construction.

Loads vault config from YAML, validates structure, and builds the lifecycle
transition table used by LifecycleService for state machine validation.
"""

from pathlib import Path

import jsonschema
import yaml
from pydantic import BaseModel, Field, PrivateAttr

from sage.instrumentation.timing import TimingConfig
from sage.models.schemas import VaultIdStr


class VaultIdentity(BaseModel):
    id: VaultIdStr = Field(description="Vault identifier. Unique within the CAS installation.")
    name: str = Field(description="Human-readable display name for this vault.")
    description: str | None = Field(
        default=None,
        description=(
            "Optional free-text description of this vault's purpose, "
            "surfaced in vault-listing UIs (e.g., the CAS Application "
            "dashboard). Null or absent when not set."
        ),
    )
    owner: str = Field(description="User or team that owns this vault.")
    storage_root: str = Field(
        description=(
            "Root directory for source files managed by this vault. "
            "Absolute path. Must be outside cloud-synced directories."
        )
    )
    brain_root: str = Field(
        description=(
            "Directory containing this vault's content store and graph "
            "store databases. Absolute path. Must be outside cloud-synced "
            "directories (SQLite and LanceDB are incompatible with cloud "
            "sync)."
        )
    )
    visibility: str = Field(description="Access scope for this vault.")
    members: list[dict] | None = Field(
        default=None,
        description=(
            "Users with access to this vault. Each entry specifies a user "
            "and their vault-level role."
        ),
    )
    timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone name (e.g., 'America/Chicago') used to derive "
            "document_date from source_modified_at when no filename or "
            "caller-supplied date is available. Defaults to UTC."
        ),
    )


class LifecycleTransition(BaseModel):
    from_state: str = Field(
        description=("Source state. Use '(new)' for the initial ingestion transition.")
    )
    action: str = Field(
        description=(
            "The action that triggers this transition. Base actions: "
            "ingest, supersede, complete, archive, reactivate."
        )
    )
    to_state: str = Field(description="Target state after the transition.")
    semantics: str | None = Field(
        default=None,
        description=(
            "What this transition means. Key distinction: supersede means "
            "a newer version exists (supersedes edge created); complete "
            "means work is done but no replacement was needed."
        ),
    )
    creates_edge: str | None = Field(
        default=None,
        description=(
            "If this transition automatically creates a graph edge, the "
            "edge_type value. The supersede action creates a 'supersedes' "
            "edge from the new version to the old."
        ),
    )


class LifecycleState(BaseModel):
    value: str = Field(description="Machine identifier for the lifecycle state.")
    label: str = Field(description="Human-readable display name.")
    description: str | None = Field(
        default=None,
        description=(
            "Semantics of this state. For base states: active (in the "
            "system), completed (work done, no replacement), archived "
            "(long-term storage; includes documents superseded by newer "
            "versions)."
        ),
    )
    is_terminal: bool = Field(
        default=False,
        description=(
            "Whether this state represents an end state from which no "
            "further transitions are expected under normal operation. "
            "Archived is terminal by default but reactivation is permitted "
            "as an exceptional case."
        ),
    )


class LifecycleConfig(BaseModel):
    states: list[LifecycleState] = Field(
        description=(
            "All lifecycle states available in this vault, including the "
            "base set and any domain-specific additions."
        )
    )
    transitions: list[LifecycleTransition] = Field(
        description=(
            "Valid state transitions. SAGE enforces transition validity; "
            "the orchestration layer determines when to trigger transitions."
        )
    )
    base_states_required: bool = Field(
        default=True,
        description=(
            "The base state set (active, completed, archived) and base "
            "transitions (ingest, supersede, complete, archive, reactivate) "
            "are always present. Domain-specific states and transitions "
            "extend this base; they do not replace it."
        ),
    )


class AbstractionConfig(BaseModel):
    enabled: bool = Field(
        default=True,
        description=(
            "Whether to generate semantic abstracts during ingestion. If "
            "false, ingestion completes with pipeline_status "
            "'abstraction_skipped'. If true and the LLM fails at runtime, "
            "pipeline_status is 'failed' (strict quality gate)."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "LLM model identifier for abstract generation. Should be a local model for Phase 1."
        ),
    )
    max_abstract_tokens: int = Field(
        default=1500,
        description=(
            "Upper bound on abstract length in tokens. Actual length is density-proportional."
        ),
    )
    base_abstract_tokens: int = Field(
        default=150,
        description=(
            "Minimum token floor for the density-proportional abstract "
            "budget. Combined with tokens_per_word as "
            "`min(base_abstract_tokens + word_count * tokens_per_word, "
            "max_abstract_tokens)` to set the per-document budget."
        ),
    )
    tokens_per_word: float = Field(
        default=0.02,
        description=(
            "Per-word scaling factor for the density-proportional abstract "
            "budget. Combined with base_abstract_tokens as "
            "`min(base_abstract_tokens + word_count * tokens_per_word, "
            "max_abstract_tokens)` to set the per-document budget."
        ),
    )


class RetrievalHealthConfig(BaseModel):
    assertions_file: str | None = Field(
        default=None,
        description=(
            "Path to a YAML file containing retrieval health assertions, "
            "relative to the domain configuration directory. Each "
            "assertion specifies a query, an expected document ID, and a "
            "top-k threshold."
        ),
    )


class DocTypeEntry(BaseModel):
    value: str = Field(
        description=(
            "Machine identifier for the doc_type. Lowercase, "
            "underscore-separated. Used in metadata fields, filename "
            "extraction maps, and pipeline stage scoping."
        )
    )
    label: str = Field(description="Human-readable display name for this document type.")
    description: str | None = Field(
        default=None,
        description=("Brief description of what this document type represents in the domain."),
    )
    source_types: list[str] | None = Field(
        default=None,
        description=(
            "Optional constraint restricting this doc_type to files "
            "ingested via specific source adapters. When present, "
            "filename-based doc_type resolution skips this type for files "
            "whose adapter is not in the list. Omit for doc_types valid "
            "across all adapters."
        ),
    )
    metadata_schema: dict | None = Field(
        default=None,
        description=(
            "Optional JSON Schema (draft 2020-12) fragment validating the "
            "structure of `tier3_metadata` payloads for documents of this "
            "doc_type. When omitted, this doc_type does not accept "
            "`tier3_metadata` (ingest/update with non-null payload returns "
            "400 `tier3_schema_violation`). When present, payloads are "
            "validated at the SAGE API boundary on `sage_ingest` and "
            "`sage_update_metadata`. The fragment is not recursively "
            "validated by SAGE; SAGE assumes the author supplied valid "
            "JSON Schema. Tier 3 is per-doc_type structured metadata, "
            "distinct from Tier 2 (the doc_type vocabulary itself)."
        ),
    )


class DocumentTypesConfig(BaseModel):
    doc_types: list[DocTypeEntry] = Field(
        description=(
            "The set of document type values valid in this vault. Each "
            "entry defines a classification in the vault's domain "
            "vocabulary."
        )
    )


class VaultConfig(BaseModel):
    """Root configuration for a SAGE vault."""

    vault: VaultIdentity = Field(description="Vault identity and storage configuration.")
    document_types: DocumentTypesConfig = Field(
        description="Document type definitions for this vault's domain vocabulary."
    )
    lifecycle: LifecycleConfig = Field(
        description="Lifecycle state machine configuration for this vault."
    )
    source_adapters: dict = Field(description="Source adapter registry and configuration.")
    metadata_extraction: dict = Field(description="Metadata extraction pipeline configuration.")
    edge_inference: dict = Field(description="Edge inference tier assignments and rules.")
    access_control_defaults: dict | None = Field(
        default=None,
        description=("Default access control settings for new documents in this vault."),
    )
    abstraction: AbstractionConfig = Field(
        default_factory=AbstractionConfig,
        description=(
            "Configuration for the semantic abstract generation stage of "
            "the ingestion pipeline (CAS-ADR-011)."
        ),
    )
    retrieval_health: RetrievalHealthConfig | None = Field(
        default=None,
        description=("Configuration for retrieval health smoke tests (eval_retrieval endpoint)."),
    )
    timing: TimingConfig = Field(
        default_factory=TimingConfig,
        description=(
            "Per-vault query-timing instrumentation configuration (T-0073). "
            "Enables structured timing records on the storage, content, "
            "and retrieval layers, written to a per-vault rotating log "
            "file under brain_root."
        ),
    )

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
