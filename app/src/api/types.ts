// TypeScript interfaces matching SAGE Core API and application backend response shapes.

// --- Vault ---

export interface VaultSummary {
  id: string;
  name: string;
  description: string | null;
  storage_root: string;
  doc_types: DocTypeEntry[];
  lifecycle_states: LifecycleState[];
  adapters: AdapterInfo[];
  projects: string[];
}

export interface DocTypeEntry {
  value: string;
  label: string;
}

export interface LifecycleState {
  value: string;
  label: string;
  is_terminal: boolean;
}

export interface AdapterInfo {
  source_type: string;
  enabled: boolean;
  extensions: string[];
}

// --- Vault Stats ---

export interface VaultStats {
  total_documents: number;
  by_lifecycle_state: Record<string, number>;
  by_doc_type: Record<string, number>;
  by_source_adapter: Record<string, number>;
  total_edges: number;
  by_edge_type: Record<string, number>;
  staging_edge_count: number;
  lancedb_size_bytes: number;
  sqlite_size_bytes: number;
  last_ingestion_at: string | null;
  health: HealthIndicators;
}

export interface HealthIndicators {
  pending_metadata_count: number;
  pending_edge_count: number;
  deferred_abstract_count: number | null;
  failed_ingestion_count: number;
}

// --- Document ---

export interface Document {
  id: string;
  title: string;
  source_type: string;
  source_path: string;
  lifecycle_status: string;
  version_label: string | null;
  project: string | null;
  tags: string[];
  authority_scope: string | null;
  doc_type: string | null;
  source_content_hash: string;
  adapter_version: string;
  created_by: string;
  created_at: string;
  last_modified_by: string;
  updated_at: string;
  projected_at: string | null;
  indexed_at: string | null;
  source_modified_at: string | null;
  document_date: string | null;
  semantic_abstract: string | null;
  pipeline_status: string;
  pipeline_error: string | null;
  tier3_metadata: Record<string, unknown> | null;
  metadata_confirmed?: boolean;
  projection_text?: string;
}

export interface DocumentSummary {
  id: string;
  title: string;
  lifecycle_status: string;
  source_type: string;
  source_path: string | null;
  version_label: string | null;
  project: string | null;
  doc_type: string | null;
  tags: string[];
  document_date: string | null;
  source_modified_at: string | null;
}

// --- Edge ---

export interface Edge {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  created_at: string;
  notes: string | null;
  rationale: string | null;
}

export interface StagingEdge {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  inference_evidence: string;
  confidence_tier: number;
  created_at: string;
}

// --- Search / Discover ---

export interface DiscoverHit {
  document: DocumentSummary;
  chunk_content: string | null;
  heading_path: string | null;
  relevance_score: number | null;
}

export interface DiscoverRequest {
  mode: 'semantic' | 'keyword' | 'deterministic' | 'catalog';
  query?: string;
  scope?: string;
  filters?: {
    doc_type?: string;
    lifecycle_status?: string;
    pipeline_status?: string;
    tags?: string[];
    project?: string;
  };
  limit?: number;
  offset?: number;
  use_hybrid?: boolean;
  sort_by?: 'title' | 'doc_type' | 'document_date' | 'lifecycle_status';
  sort_order?: 'asc' | 'desc';
}

export interface DiscoverResponse {
  mode: string;
  results: DiscoverHit[];
  total_available: number;
  cursor: string | null;
}

// --- Graph ---

export interface TraversalNode {
  document: DocumentSummary;
  edge: Edge;
  depth: number;
  edge_counts: Record<string, number>;
}

export interface ChainEntry {
  id: string;
  title: string;
  version_label: string | null;
  lifecycle_status: string;
  document_date: string | null;
  position: number;
}

export interface ChainResponse {
  chain: ChainEntry[];
  head_id: string;
  tail_id: string;
  query_position: number;
  length: number;
  is_linear: boolean;
}

export interface TraverseRequest {
  start_id: string;
  edge_type?: string;
  direction?: 'outbound' | 'inbound' | 'both';
  depth?: number;
}

export interface TraverseResponse {
  start_id: string;
  nodes: TraversalNode[];
}

export interface LinkRequest {
  source_id: string;
  target_id: string;
  edge_type: string;
  notes?: string;
  rationale?: string;
}

// --- Review ---

export interface PendingMetadata {
  document: Document;
  extracted_fields: Record<string, {
    value: string | null;
    source: string;
    alt_value?: string;
    alt_source?: string;
  }>;
}

// --- Ingest (app backend) ---

export interface ParsedMetadataItem {
  title: string;
  date: string | null;
  project: string | null;
  codes: string[];
  version: string | null;
  doc_type: string | null;
}

export interface ScanResultItem {
  file_path: string;
  file_hash: string;
  source_modified_at: string;
  adapter: string | null;
  parsed_metadata: ParsedMetadataItem;
  sage_status: 'new' | 'modified' | 'unchanged' | 'no_adapter';
}

export interface ScanResponse {
  files: ScanResultItem[];
  warnings: string[];
}

export interface IngestProgressEvent {
  event_type: 'progress';
  file_index: number;
  total_files: number;
  filename: string;
  stage: string;
  status: 'started' | 'completed' | 'failed';
  document_id?: string;
  error?: string;
}

export interface IngestSummaryEvent {
  event_type: 'summary';
  documents_created: { new: number; new_version: number };
  metadata_pending: number;
  edges_created: Record<string, number>;
  edges_staged: Record<string, number>;
  edges_dropped: number;
  abstracts_generated: number;
  abstracts_deferred: number;
  error_count: number;
}

// --- Vault Config ---

export interface VaultIdentityConfig {
  id: string;
  name: string;
  description: string | null;
  owner: string;
  storage_root: string;
  brain_root: string;
  visibility: string;
  members: Record<string, unknown>[] | null;
}

export interface DocTypeConfig {
  value: string;
  label: string;
  description?: string | null;
  source_types?: string[] | null;
}

export interface LifecycleStateConfig {
  value: string;
  label: string;
  description?: string | null;
  is_terminal?: boolean;
}

export interface LifecycleTransitionConfig {
  from_state: string;
  action: string;
  to_state: string;
  semantics?: string | null;
  creates_edge?: string | null;
}

export interface LifecycleConfig {
  base_states_required: boolean;
  states: LifecycleStateConfig[];
  transitions: LifecycleTransitionConfig[];
}

export interface AbstractionConfig {
  enabled: boolean;
  model: string | null;
  max_abstract_tokens: number;
}

export interface VaultConfig {
  vault: VaultIdentityConfig;
  document_types: { doc_types: DocTypeConfig[] };
  lifecycle: LifecycleConfig;
  source_adapters: Record<string, unknown>;
  metadata_extraction: Record<string, unknown>;
  edge_inference: Record<string, unknown>;
  abstraction: AbstractionConfig;
  access_control_defaults?: Record<string, unknown> | null;
  retrieval_health?: Record<string, unknown> | null;
}

export interface UpdateConfigResponse {
  status: string;
  vault_id: string;
  warnings: string[];
}

// --- Metadata update ---

export interface UpdateMetadataRequest {
  title?: string;
  version_label?: string;
  project?: string;
  tags?: string[];
  doc_type?: string;
  authority_scope?: string;
  document_date?: string;
}
