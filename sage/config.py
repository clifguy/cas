"""Vault configuration loading and transition table construction.

Loads vault config from YAML, validates structure, and builds the lifecycle
transition table used by LifecycleService for state machine validation.
"""

import logging
from pathlib import Path
from typing import Literal

import jsonschema
import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from sage.instrumentation.timing import TimingConfig
from sage.models.schemas import VaultIdStr

logger = logging.getLogger(__name__)


def pattern_is_discriminating(pattern: dict) -> bool:
    """True when an identifier_mention pattern can resolve a *specific*
    identifier.

    A pattern discriminates when it carries a ``target_tier3`` filter or a
    ``target_tags`` entry with a ``{...}`` placeholder substituted per
    identifier. A pattern with only ``target_doc_type`` and/or static tags
    matches every document of the type and cannot pick one. The resolver
    (``identifier_mention_inference._resolve_identifier``) and the config
    warning pass both key off this single predicate so they cannot drift.
    """
    return bool(pattern.get("target_tier3")) or any(
        "{" in t for t in (pattern.get("target_tags") or [])
    )


def identifier_mention_pattern_warnings(edge_inference: dict) -> list[str]:
    """Return non-fatal warnings for misconfigured identifier_mention patterns.

    The structural ``edge_inference`` JSON Schema deliberately still accepts
    legacy tag-based patterns for backward compatibility, so these defects
    cannot be caught by schema validation alone. This routine flags the two
    that silently break ADR/F-record resolution:

    * ``target_title_prefix`` -- an orphaned key. The resolver dropped
      title-prefix matching during the tier3 migration, so a pattern that
      relies on it resolves on its remaining (non-discriminating) filters.
    * a *non-discriminating* pattern -- no ``target_tier3`` and no
      placeholder-bearing ``target_tags`` entry. Such a pattern matches
      every document of ``target_doc_type`` and the resolver refuses to link
      it (see ``identifier_mention_inference._resolve_identifier``), so its
      mentions silently produce no edges.

    Warnings only; callers log them. Returning a value (rather than raising)
    keeps a vault whose config predates the tier3 migration loadable.
    """
    warnings: list[str] = []
    if not isinstance(edge_inference, dict):
        return warnings
    for assignment in edge_inference.get("tier_assignments", []) or []:
        if assignment.get("edge_type") != "references":
            continue
        for rule in assignment.get("inference_rules", []) or []:
            if rule.get("method") != "identifier_mention":
                continue
            for pattern in rule.get("patterns", []) or []:
                regex = pattern.get("regex", "<no-regex>")
                if "target_title_prefix" in pattern:
                    warnings.append(
                        f"identifier_mention pattern {regex!r} carries the "
                        "unsupported key 'target_title_prefix'; the resolver "
                        "ignores it (title-prefix matching was removed in the "
                        "tier3 migration)."
                    )
                if not pattern_is_discriminating(pattern):
                    warnings.append(
                        f"identifier_mention pattern {regex!r} is "
                        "non-discriminating (no target_tier3 and no "
                        "placeholder-bearing target_tags); it cannot resolve a "
                        "specific identifier and the resolver will refuse to "
                        "link its mentions."
                    )
    return warnings


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


class VaultAbstractionConfig(BaseModel):
    """Per-vault abstraction config (CAS-ADR-030).

    Carries the vault-scope opt-in (`enabled`) and the density-proportional
    token-budget tuning. Provider and model identification moved to stack
    scope (see `StackAbstractionConfig`).
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Whether to generate semantic abstracts during ingestion. If "
            "false, ingestion completes with pipeline_status "
            "'abstraction_skipped'. If true and the LLM fails at runtime, "
            "pipeline_status is 'failed' (strict quality gate)."
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
    max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Maximum number of attempts the abstraction worker makes for a "
            "queued document before stamping pipeline_status 'failed'. "
            "Applies only to queued work (concurrent ingest, reabstract, "
            "recompute, startup recovery); the synchronous wait-for-pipeline "
            "path remains fail-fast. The first attempt counts toward this "
            "total, so max_attempts=3 means one initial try plus two retries."
        ),
    )
    retry_backoff_base_seconds: float = Field(
        default=1.0,
        ge=0,
        description=(
            "Base delay in seconds for the abstraction worker's exponential "
            "retry backoff. The delay before retry k (1-indexed) is "
            "min(retry_backoff_base_seconds * 2**(k-1), "
            "retry_backoff_max_seconds)."
        ),
    )
    retry_backoff_max_seconds: float = Field(
        default=30.0,
        ge=0,
        description=(
            "Upper bound in seconds on the abstraction worker's retry backoff "
            "delay, capping the exponential growth so a poison document cannot "
            "stall the queue indefinitely."
        ),
    )


class StackAbstractionConfig(BaseModel):
    """Stack-wide abstraction config (CAS-ADR-030).

    Loaded once at SAGE process startup before any vault is registered.
    Carries the provider dispatch key and the model identifier passed to
    the provider's factory. Co-located with the resource boundary because
    the local MLX provider is enforced as a process-wide singleton.
    """

    provider: Literal["local-mlx", "anthropic", "stub"] = Field(
        default="local-mlx",
        description=(
            "Abstraction provider dispatch key (Abstraction Provider "
            "Evaluation Framework §3.6, re-anchored at stack scope by "
            "CAS-ADR-030). 'local-mlx' loads the local MLX provider "
            "(Qwen3) with the model identifier from the 'model' field. "
            "'anthropic' loads the hosted Claude provider with the model "
            "identifier from the 'model' field. 'stub' loads the stub "
            "provider (test/disabled stacks)."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "LLM model identifier for abstract generation, passed to the "
            "provider's factory. Required when provider is 'local-mlx' or "
            "'anthropic' (startup fails loudly if null): a local model "
            "identifier for 'local-mlx', a Claude model identifier for "
            "'anthropic'. Ignored when provider is 'stub'."
        ),
    )


class StackPostgresConfig(BaseModel):
    """Stack-wide Postgres connection parameters (CAS-ADR-042).

    Non-secret connection parameters for the Postgres storage engine the
    deployment profiles externalize durable state to. The password is read
    from the environment, never carried here. Consumed by the live storage
    binding (when ``storage_backend`` selects ``postgres``), the provisioning
    CLI, and the storage test harness.
    """

    model_config = {"extra": "forbid"}

    host: str | None = Field(
        default=None,
        description=(
            "Postgres server hostname. Null selects a local unix-domain "
            "socket connection (the on-box default: peer authentication, no "
            "password); a hostname or address selects a TCP connection (the "
            "hosted target)."
        ),
    )
    port: int = Field(
        default=5432,
        description=("Postgres server TCP port. Ignored for a unix-socket connection (host null)."),
    )
    database: str = Field(
        default="sage",
        description="Name of the database to connect to within the Postgres server.",
    )
    user: str | None = Field(
        default=None,
        description=(
            "Postgres role to connect as. Null defers to the operating-system "
            "user, which the local socket authenticates by peer."
        ),
    )
    sslmode: str | None = Field(
        default=None,
        description=(
            "libpq sslmode for the connection (for example 'require' for a "
            "managed hosted endpoint). Null leaves the libpq default and is "
            "typical for a local socket."
        ),
    )
    min_pool_size: int = Field(
        default=1,
        description="Minimum number of connections the async pool keeps open.",
    )
    max_pool_size: int = Field(
        default=10,
        description="Maximum number of connections the async pool will open.",
    )
    extensions: list[str] = Field(
        default_factory=lambda: ["vector", "pgstattuple"],
        description=(
            "Postgres extensions the schema bootstrap enables in the target "
            "database. 'vector' (pgvector) is required for the content store's "
            "embedding column; 'pgstattuple' backs bloat measurement. "
            "'pg_repack' is documented for the local runtime but kept out of "
            "this default because managed and containerized targets may not "
            "carry it."
        ),
    )


class StackDocumentStoreConfig(BaseModel):
    """Coordinates for the cloud document-store vault-source binding (CAS-ADR-043).

    Non-secret SharePoint / Microsoft Graph coordinates the document-store
    binding addresses when ``vault_source_backend`` selects ``document_store``:
    the single site that hosts the vault tree, the document library (drive)
    within it, and the folder under that library the vault tree is rooted at.
    Authentication is the workload's managed identity under an application
    permission scoped to this one site (least privilege); no secret is carried
    here. Consumed by the live vault-source binding and its Graph client.
    """

    model_config = {"extra": "forbid"}

    site_id: str | None = Field(
        default=None,
        description=(
            "Microsoft Graph site identifier of the single SharePoint site "
            "that hosts the vault tree. The application permission is scoped to "
            "this one site (least privilege), never a tenant-wide grant. "
            "Required when 'vault_source_backend' is 'document_store'."
        ),
    )
    drive_id: str | None = Field(
        default=None,
        description=(
            "Microsoft Graph drive identifier of the document library within "
            "the site that holds the vault tree. Required when "
            "'vault_source_backend' is 'document_store'."
        ),
    )
    root_path: str = Field(
        default="vaults",
        description=(
            "Folder path within the document library the vault tree is rooted "
            "at; each vault's configuration declaration is persisted at "
            "'<root_path>/<vault_id>/vault_config.yaml'. Empty selects the "
            "library root."
        ),
    )
    graph_scope: str = Field(
        default="https://graph.microsoft.com/.default",
        description=(
            "OAuth scope the managed-identity credential requests when minting "
            "the Microsoft Graph access token. The '.default' scope grants the "
            "application permissions already consented for the workload's "
            "identity."
        ),
    )


class StackAuthConfig(BaseModel):
    """Stack-wide OAuth resource-server configuration (CAS-ADR-042).

    Binds the SAGE process as an OAuth resource server: it carries the
    issuer and audience a bearer token is validated against and the
    scope/role an accepted token must carry. Present and ``enabled`` only
    where the deployment authenticates its callers; the on-box default
    leaves it absent, so that deployment runs with no auth. A deployment
    fronted by an external identity provider populates it. The delegated
    user identity is carried by the caller's bearer token; the process
    never authenticates as a service principal.

    Non-secret directory coordinates only (tenant, audience, issuer); the
    token-signing keys are fetched from the issuer's published JWKS
    endpoint at runtime, never carried here.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. When false (the default) the process "
            "authenticates no one. When true it validates a bearer token on "
            "every request to a protected surface against the issuer and "
            "audience below."
        ),
    )
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Identity-provider tenant identifier. Derives the default issuer "
            "and JWKS endpoint when those are not set explicitly. Required "
            "when 'enabled' is true and 'issuer'/'jwks_uri' are not both given."
        ),
    )
    audience: str | None = Field(
        default=None,
        description=(
            "Expected token audience -- the resource server's application ID "
            "URI (for example 'api://<app-id>'). A token whose 'aud' claim "
            "does not equal this value is rejected. Required when 'enabled' "
            "is true."
        ),
    )
    issuer: str | None = Field(
        default=None,
        description=(
            "Expected token issuer. When null it is derived from 'tenant_id' "
            "as 'https://login.microsoftonline.com/<tenant_id>/v2.0'. A token "
            "whose 'iss' claim does not equal the resolved issuer is rejected."
        ),
    )
    jwks_uri: str | None = Field(
        default=None,
        description=(
            "Endpoint publishing the issuer's token-signing public keys. When "
            "null it is derived from 'tenant_id' as "
            "'https://login.microsoftonline.com/<tenant_id>/discovery/v2.0/keys'. "
            "Signing keys are fetched and cached from here at runtime."
        ),
    )
    required_scopes: list[str] = Field(
        default_factory=lambda: ["Sage.Access"],
        description=(
            "Delegated scopes (the token's space-delimited 'scp' claim) of "
            "which an accepted token must carry at least one. The delegated "
            "scope is the authorization unit for every surface uniformly; a "
            "token carrying neither a required scope nor a required role is "
            "rejected as insufficiently authorized."
        ),
    )
    required_roles: list[str] = Field(
        default_factory=lambda: ["Sage.Reader"],
        description=(
            "Application roles (the token's 'roles' claim) of which an "
            "accepted token may instead carry at least one -- the app-only "
            "complement to 'required_scopes'. An empty list disables "
            "role-based acceptance."
        ),
    )
    resource_metadata_url: str | None = Field(
        default=None,
        description=(
            "Optional protected-resource-metadata URL advertised in the "
            "'WWW-Authenticate' challenge so a client can discover how to "
            "obtain a token. When null the challenge omits the pointer; the "
            "metadata document itself is served by the edge facade, not by "
            "this process."
        ),
    )

    def resolved_issuer(self) -> str | None:
        """Issuer to validate against: the explicit value, else derived from tenant."""
        if self.issuer is not None:
            return self.issuer
        if self.tenant_id is not None:
            return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        return None

    def resolved_jwks_uri(self) -> str | None:
        """JWKS endpoint: the explicit value, else derived from tenant."""
        if self.jwks_uri is not None:
            return self.jwks_uri
        if self.tenant_id is not None:
            return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        return None


class SageCoreConfig(BaseModel):
    """Root configuration for the SAGE Core API process (CAS-ADR-030).

    Loaded once at process startup from `sage/config.yaml` before any vault
    is registered. Carries resources whose enforcement spans the whole
    SAGE process; per-vault configuration lives in `VaultConfig`.
    """

    profile: Literal["local", "cloud"] = Field(
        default="local",
        description=(
            "Deployment-profile marker (CAS-ADR-042). A deployment profile "
            "is the single stack-scope selection that co-binds the "
            "adapter-port implementations for one deployment target; the "
            "layer above the ports stays profile-invariant. The resolver "
            "assembles the profile's bindings once at stack startup: 'local' "
            "(the on-box, single-process deployment whose bindings read their "
            "secrets from the environment) or 'cloud' (the hosted, "
            "containerized deployment whose bindings resolve their secrets from "
            "the managed secret store via the workload's managed identity). An "
            "unrecognized value fails loud at startup."
        ),
    )
    storage_backend: Literal["postgres", "embedded"] = Field(
        default="postgres",
        description=(
            "Durable-storage binding selector (CAS-ADR-042). 'postgres' (the "
            "default) binds the graph and content stores to the Postgres "
            "adapters configured by the 'postgres' block; each vault's rows "
            "live in a Postgres schema named by its vault id. 'embedded' "
            "selects the file-based fallback pair -- SQLite graph store and "
            "LanceDB content store under each vault's brain root. The two "
            "stores co-vary as one binding and are not selectable "
            "individually. Flipping the key repoints the binding at restart "
            "and never moves data; copying state between backends is the "
            "operator migration tooling's job."
        ),
    )
    vault_source_backend: Literal["filesystem", "document_store"] = Field(
        default="filesystem",
        description=(
            "Vault-source-store binding selector (CAS-ADR-043). The "
            "vault-source store persists the vault's configuration declaration "
            "and the source files retained from each ingest -- the one durable "
            "seam CAS-ADR-042 left on the local filesystem. 'filesystem' (the "
            "default) binds it to the local vault tree under the vault root: "
            "today's behavior, durable on the on-box target. 'document_store' "
            "selects the tenant-native external binding for a stateless "
            "deployment, where the local filesystem is ephemeral. The two "
            "bindings are independently selectable from either deployment "
            "profile (the co-variation test), so either environment can run "
            "against either store. Like the storage backend, flipping the key "
            "repoints the binding at restart and never moves data."
        ),
    )
    abstraction: StackAbstractionConfig = Field(
        default_factory=StackAbstractionConfig,
        description=(
            "Stack-wide abstraction provider configuration (CAS-ADR-030). "
            "The provider and model identifier are SAGE-process-wide "
            "because the local MLX provider is enforced as a singleton across "
            "the process; co-locating the config with the resource "
            "boundary avoids the layering contradiction where a per-vault "
            "field controls a stack-wide resource. Per-vault opt-in and "
            "token-budget tuning live in the vault config's abstraction "
            "block."
        ),
    )
    postgres: StackPostgresConfig = Field(
        default_factory=StackPostgresConfig,
        description=(
            "Connection parameters for the Postgres storage engine "
            "(CAS-ADR-042). Both deployment targets externalize durable graph "
            "and content state to a networked relational engine -- a local "
            "unix socket on the on-box target, a managed endpoint on the "
            "hosted target -- with the embedded SQLite/LanceDB stores "
            "retained as a fallback binding. Non-secret parameters only: the "
            "password is read from the environment (SAGE_PG_PASSWORD), never "
            "from this file. The block configures the async driver and "
            "connection pool the store adapters share and the idempotent "
            "schema bootstrap; it is consumed by the live storage binding "
            "(when 'storage_backend' selects 'postgres'), the provisioning "
            "CLI, and the storage test harness."
        ),
    )
    document_store: StackDocumentStoreConfig = Field(
        default_factory=StackDocumentStoreConfig,
        description=(
            "Coordinates for the cloud document-store vault-source binding "
            "(CAS-ADR-043). Consumed by the live vault-source binding when "
            "'vault_source_backend' selects 'document_store': the SharePoint "
            "site, document library, and root folder the vault tree is "
            "persisted to, addressed over Microsoft Graph under the workload's "
            "managed identity. Non-secret coordinates only; the binding "
            "authenticates with the managed identity and carries no secret. "
            "Ignored when the filesystem binding is selected."
        ),
    )
    auth: StackAuthConfig | None = Field(
        default=None,
        description=(
            "OAuth resource-server binding (CAS-ADR-042). Absent (the "
            "default) installs a pass-through validator -- the on-box "
            "deployment runs with no auth. Present and enabled, it binds the "
            "process as an OAuth resource server that validates an "
            "Entra-issued bearer token uniformly across the REST and MCP "
            "surfaces against the configured issuer and audience. Non-secret "
            "directory coordinates only; token-signing keys are fetched from "
            "the issuer's published JWKS endpoint at runtime. Resolved "
            "once at startup through the deployment profile's auth seam."
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
            "validated at the SAGE API boundary on `ingest_document` and "
            "`update_metadata`. The fragment is not recursively "
            "validated by SAGE; SAGE assumes the author supplied valid "
            "JSON Schema. Tier 3 is per-doc_type structured metadata, "
            "distinct from Tier 2 (the doc_type vocabulary itself)."
        ),
    )
    unique_keys: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of tier3_metadata field names whose values must "
            "be unique within this doc_type across all lifecycle statuses "
            "(CAS-ADR-031). Each entry must name a top-level property "
            "declared in `metadata_schema.properties`. Uniqueness is "
            "enforced atomically by the SAGE storage substrate on insert "
            "and supersession-insert; a write that would violate the "
            "constraint raises `tier3_unique_constraint_violation` (409). "
            "The supersession chain is the explicit exception: a successor "
            "inherits its predecessor's identifier without collision. A "
            "doc_type with no `unique_keys` (or an empty list) has no "
            "uniqueness constraint on any tier3 field. Activating a "
            "`unique_keys` declaration requires a clean portfolio per "
            "CAS-ADR-031 §5; SAGE refuses to create the underlying "
            "index while collisions exist."
        ),
    )

    @model_validator(mode="after")
    def _validate_unique_keys_reference_metadata_schema(self) -> "DocTypeEntry":
        """Each `unique_keys` entry must name a property declared in
        `metadata_schema.properties`. A `unique_keys` declaration on a
        doc_type with no `metadata_schema` (or no matching property) is a
        configuration error — the constraint would target a field SAGE
        doesn't validate the shape of.
        """
        if not self.unique_keys:
            return self
        schema_props: set[str] = set()
        if self.metadata_schema is not None:
            props = self.metadata_schema.get("properties")
            if isinstance(props, dict):
                schema_props = set(props.keys())
        missing = [k for k in self.unique_keys if k not in schema_props]
        if missing:
            raise ValueError(
                f"doc_type '{self.value}' declares unique_keys {missing} that "
                f"are not properties of its metadata_schema "
                f"(declared properties: {sorted(schema_props) or 'none'})"
            )
        return self


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
    abstraction: VaultAbstractionConfig = Field(
        default_factory=VaultAbstractionConfig,
        description=(
            "Per-vault configuration for the semantic abstract generation "
            "stage of the ingestion pipeline (CAS-ADR-011). Carries the "
            "vault-scope opt-in (`enabled`) and the density-proportional "
            "token-budget tuning. Provider and model identification are "
            "SAGE-process-wide (CAS-ADR-030) and live under the stack "
            "config, not here."
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
        for warning in identifier_mention_pattern_warnings(self.edge_inference):
            logger.warning("vault '%s': %s", self.vault.id, warning)

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
        the implementation plan).
        """
        return self._tier3_validators.get(doc_type)


class TransitionTable:
    """Lookup structure for lifecycle state machine validation.

    Built from the vault's lifecycle transition list. Queryable by
    current_state to get valid actions, or by (current_state, action)
    to get the target state.
    """

    def __init__(self, transitions: list[LifecycleTransition]) -> None:
        # {from_state: [(action, to_state, creates_edge),...]}
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


_SAGE_CORE_CONFIG_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "fs" / "sage" / "sage_core_config.schema.json"
)


def load_sage_core_config(config_path: Path) -> SageCoreConfig:
    """Load and validate the SAGE Core API stack config from a YAML file.

    Validates the raw dict against `docs/fs/sage/sage_core_config.schema.json`
    before constructing the Pydantic model, so a misnamed key or unexpected
    type surfaces against the formal substrate (CAS-ADR-008) rather than as
    a less-actionable Pydantic error. Returns the constructed `SageCoreConfig`.
    """
    import json as _json

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    schema = _json.loads(_SAGE_CORE_CONFIG_SCHEMA_PATH.read_text())
    jsonschema.validate(raw, schema)
    return SageCoreConfig.model_validate(raw)
