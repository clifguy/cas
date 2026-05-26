// Mock data mirroring SAGE API response shapes.
// All data is static; no API calls are made in the wireframe.

// --- Types (mirroring sage/models/schemas.py) ---

export interface VaultIdentity {
  id: string;
  name: string;
  owner: string;
  description: string;
  storage_root: string;
  brain_root: string;
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
  version: string;
}

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
  semantic_abstract: string | null;
  pipeline_status: string;
  pipeline_error: string | null;
  tier3_metadata: Record<string, unknown> | null;
  projection_text?: string;
}

export interface DocumentSummary {
  id: string;
  title: string;
  lifecycle_status: string;
  source_type: string;
  version_label: string | null;
  project: string | null;
  doc_type: string | null;
  tags: string[];
}

export interface Edge {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  created_at: string;
  notes: string | null;
  rationale: string | null;
}

export interface StagingEdge extends Edge {
  inference_evidence: string;
  confidence_tier: number;
}

export interface DiscoverHit {
  document: DocumentSummary;
  chunk_content: string | null;
  heading_path: string | null;
  relevance_score: number | null;
}

export interface PendingMetadata {
  document: Document;
  extracted_fields: Record<string, {
    value: string;
    source: 'filename' | 'content' | 'default';
    alt_value?: string;
    alt_source?: string;
  }>;
}

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
  source_type: string | null;
  parsed_metadata: ParsedMetadataItem;
  sage_status: 'new' | 'modified' | 'unchanged' | 'no_adapter';
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

export interface VaultData {
  identity: VaultIdentity;
  doc_types: DocTypeEntry[];
  lifecycle_states: LifecycleState[];
  adapters: AdapterInfo[];
  documents: Document[];
  edges: Edge[];
  staging_edges: StagingEdge[];
  pending_metadata: PendingMetadata[];
  search_results: DiscoverHit[];
  mock_scan_results: ScanResultItem[];
}

// --- Mock Data ---

const exampleDocuments: Document[] = [
  {
    id: 'doc-001',
    title: 'System Architecture Overview',
    source_type: 'docx',
    source_path: '/path/to/example_vault/PIM_Architecture_Overview_v2.docx',
    lifecycle_status: 'active',
    version_label: 'v2',
    project: 'example_vault',
    tags: ['architecture', 'overview'],
    authority_scope: null,
    doc_type: 'technical_disclosure',
    source_content_hash: 'sha256:abc123def456',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-03-15T10:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-04-01T14:30:00Z',
    projected_at: '2026-03-15T10:01:00Z',
    indexed_at: '2026-03-15T10:02:00Z',
    source_modified_at: '2026-03-14T16:00:00Z',
    semantic_abstract: 'Describes the layered architecture of the example platform, covering data ingestion, processing pipelines, and API design patterns.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: { heading_styles: ['Heading 1', 'Heading 2', 'Heading 3'], page_count: 24 },
    projection_text: '# System Architecture Overview\n\n## 1. Introduction\n\nThe example platform is a modular system...\n\n## 2. Data Layer\n\nAll patient interaction data flows through...\n\n## 3. Processing Pipeline\n\nThe pipeline consists of three stages...\n\n## 4. API Design\n\nRESTful endpoints expose...',
  },
  {
    id: 'doc-002',
    title: 'System Architecture Overview',
    source_type: 'docx',
    source_path: '/path/to/example_vault/PIM_Architecture_Overview_v1.docx',
    lifecycle_status: 'archived',
    version_label: 'v1',
    project: 'example_vault',
    tags: ['architecture', 'overview'],
    authority_scope: null,
    doc_type: 'technical_disclosure',
    source_content_hash: 'sha256:old789xyz',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-02-10T09:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-15T10:00:00Z',
    projected_at: '2026-02-10T09:01:00Z',
    indexed_at: '2026-02-10T09:02:00Z',
    source_modified_at: '2026-02-09T11:00:00Z',
    semantic_abstract: 'Earlier version of the example architecture document covering initial design decisions.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: null,
  },
  {
    id: 'doc-003',
    title: 'CD-04 Interaction Context Design Spec',
    source_type: 'docx',
    source_path: '/path/to/example_vault/2026-03-01_PIM_CD-04_InteractionContext_v1.docx',
    lifecycle_status: 'active',
    version_label: 'v1',
    project: 'example_vault',
    tags: ['design', 'interaction-context'],
    authority_scope: 'CD-04',
    doc_type: 'design_spec',
    source_content_hash: 'sha256:designhash456',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-03-01T11:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-20T16:00:00Z',
    projected_at: '2026-03-01T11:01:00Z',
    indexed_at: '2026-03-01T11:02:00Z',
    source_modified_at: '2026-02-28T15:00:00Z',
    semantic_abstract: 'Design specification for the Interaction Context system, covering contextual data capture from provider-side service encounters.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: { heading_styles: ['Heading 1', 'Heading 2'], page_count: 38 },
    projection_text: '# CD-04 Interaction Context\n\n## Abstract\n\nA system and method for capturing contextual data...\n\n## Claims\n\n### Claim 1\n\nA computer-implemented method comprising...',
  },
  {
    id: 'doc-004',
    title: 'Integration Catalog',
    source_type: 'markdown',
    source_path: '/path/to/example_vault/PIM_Integration_Catalog.md',
    lifecycle_status: 'active',
    version_label: null,
    project: 'example_vault',
    tags: ['integration', 'catalog'],
    authority_scope: null,
    doc_type: 'reference',
    source_content_hash: 'sha256:intcat789',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-03-10T08:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-10T08:00:00Z',
    projected_at: '2026-03-10T08:01:00Z',
    indexed_at: '2026-03-10T08:02:00Z',
    source_modified_at: '2026-03-09T17:00:00Z',
    semantic_abstract: 'Catalog of all integration points in the example platform, including APIs, webhooks, and data exchange formats.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: null,
    projection_text: '# Integration Catalog\n\n## EHR Integration\n\nThe platform connects to EHR systems via...\n\n## Webhook Events\n\nThe following events trigger outbound webhooks...',
  },
  {
    id: 'doc-005',
    title: 'Weekly Status Report 2026-W13',
    source_type: 'markdown',
    source_path: '/path/to/example_vault/status/2026-W13_Status.md',
    lifecycle_status: 'active',
    version_label: null,
    project: 'example_vault',
    tags: ['status', 'weekly'],
    authority_scope: null,
    doc_type: 'status_report',
    source_content_hash: 'sha256:status13',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-03-28T17:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-28T17:00:00Z',
    projected_at: '2026-03-28T17:01:00Z',
    indexed_at: '2026-03-28T17:02:00Z',
    source_modified_at: '2026-03-28T16:55:00Z',
    semantic_abstract: null,
    pipeline_status: 'abstraction_skipped',
    pipeline_error: null,
    tier3_metadata: null,
  },
  {
    id: 'doc-006',
    title: 'CD-01 Data Mesh Design Spec',
    source_type: 'docx',
    source_path: '/path/to/example_vault/2026-02-15_PIM_CD-01_DataMesh_v1.docx',
    lifecycle_status: 'active',
    version_label: 'v1',
    project: 'example_vault',
    tags: ['design', 'data-mesh'],
    authority_scope: 'CD-01',
    doc_type: 'design_spec',
    source_content_hash: 'sha256:cd01hash',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-02-15T10:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-05T09:00:00Z',
    projected_at: '2026-02-15T10:01:00Z',
    indexed_at: '2026-02-15T10:02:00Z',
    source_modified_at: '2026-02-14T18:00:00Z',
    semantic_abstract: 'Design draft for the data mesh architecture enabling federated data access across provider networks.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: { heading_styles: ['Heading 1', 'Heading 2'], page_count: 42 },
  },
  {
    id: 'doc-007',
    title: 'Meeting Notes - Strategy Review',
    source_type: 'markdown',
    source_path: '/path/to/example_vault/notes/2026-04-02_Strategy_Review.md',
    lifecycle_status: 'draft',
    version_label: null,
    project: 'example_vault',
    tags: ['meeting', 'strategy'],
    authority_scope: null,
    doc_type: 'meeting_notes',
    source_content_hash: 'sha256:meeting402',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-04-02T15:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-04-02T15:00:00Z',
    projected_at: '2026-04-02T15:01:00Z',
    indexed_at: null,
    source_modified_at: '2026-04-02T14:50:00Z',
    semantic_abstract: null,
    pipeline_status: 'failed',
    pipeline_error: 'Indexing failed: LanceDB write timeout after 30s',
    tier3_metadata: null,
  },
  {
    id: 'doc-008',
    title: 'Glossary',
    source_type: 'markdown',
    source_path: '/path/to/example_vault/PIM_Glossary.md',
    lifecycle_status: 'active',
    version_label: null,
    project: 'example_vault',
    tags: ['glossary', 'terminology'],
    authority_scope: null,
    doc_type: 'reference',
    source_content_hash: 'sha256:glossary1',
    adapter_version: '1.0.0',
    created_by: 'clif',
    created_at: '2026-01-20T10:00:00Z',
    last_modified_by: 'clif',
    updated_at: '2026-03-25T11:00:00Z',
    projected_at: '2026-01-20T10:01:00Z',
    indexed_at: '2026-01-20T10:02:00Z',
    source_modified_at: '2026-01-19T09:00:00Z',
    semantic_abstract: 'Authoritative glossary of terms used across the example portfolio and technical documentation.',
    pipeline_status: 'abstraction_complete',
    pipeline_error: null,
    tier3_metadata: null,
    projection_text: '# Glossary\n\n## A\n\n**Adaptive Learning Engine** - The subsystem responsible for...\n\n## C\n\n**Clinical Decision Support** - Real-time guidance...',
  },
];

const exampleEdges: Edge[] = [
  {
    id: 'edge-001',
    source_id: 'doc-001',
    target_id: 'doc-002',
    edge_type: 'supersedes',
    created_at: '2026-03-15T10:00:00Z',
    notes: 'v2 supersedes v1',
    rationale: 'Version chain via re-ingestion',
  },
  {
    id: 'edge-002',
    source_id: 'doc-003',
    target_id: 'doc-001',
    edge_type: 'derived_from',
    created_at: '2026-03-01T11:00:00Z',
    notes: null,
    rationale: 'Design draft references architecture document',
  },
  {
    id: 'edge-003',
    source_id: 'doc-004',
    target_id: 'doc-001',
    edge_type: 'covers',
    created_at: '2026-03-10T08:05:00Z',
    notes: 'Integration catalog covers architecture integration points',
    rationale: null,
  },
  {
    id: 'edge-004',
    source_id: 'doc-006',
    target_id: 'doc-001',
    edge_type: 'derived_from',
    created_at: '2026-02-15T10:05:00Z',
    notes: null,
    rationale: 'Design draft references architecture',
  },
  {
    id: 'edge-005',
    source_id: 'doc-003',
    target_id: 'doc-006',
    edge_type: 'bundles_with',
    created_at: '2026-03-01T11:05:00Z',
    notes: 'Both design specs in the same portfolio',
    rationale: null,
  },
  {
    id: 'edge-006',
    source_id: 'doc-008',
    target_id: 'doc-003',
    edge_type: 'references',
    created_at: '2026-03-25T11:00:00Z',
    notes: 'Glossary provides term definitions used in design spec',
    rationale: null,
  },
];

const exampleStagingEdges: StagingEdge[] = [
  {
    id: 'staging-001',
    source_id: 'doc-005',
    target_id: 'doc-003',
    edge_type: 'covers',
    created_at: '2026-03-28T17:05:00Z',
    notes: null,
    rationale: null,
    inference_evidence: 'Status report mentions CD-04 design code in body text',
    confidence_tier: 2,
  },
  {
    id: 'staging-002',
    source_id: 'doc-005',
    target_id: 'doc-006',
    edge_type: 'covers',
    created_at: '2026-03-28T17:05:00Z',
    notes: null,
    rationale: null,
    inference_evidence: 'Status report mentions CD-01 design code in body text',
    confidence_tier: 2,
  },
  {
    id: 'staging-003',
    source_id: 'doc-004',
    target_id: 'doc-008',
    edge_type: 'derived_from',
    created_at: '2026-03-10T08:10:00Z',
    notes: null,
    rationale: null,
    inference_evidence: 'Filename co-location: both in vault root directory',
    confidence_tier: 2,
  },
];

const examplePendingMetadata: PendingMetadata[] = [
  {
    document: exampleDocuments[4], // Weekly Status Report
    extracted_fields: {
      project: { value: 'example_vault', source: 'filename' },
      doc_type: { value: 'status_report', source: 'filename' },
      version_label: { value: 'W13', source: 'filename', alt_value: 'Week 13', alt_source: 'content' },
    },
  },
  {
    document: exampleDocuments[6], // Meeting Notes
    extracted_fields: {
      project: { value: 'example_vault', source: 'default' },
      doc_type: { value: 'meeting_notes', source: 'filename' },
      title: { value: 'Strategy Review', source: 'filename', alt_value: 'Strategy Session - Q2 Planning', alt_source: 'content' },
    },
  },
];

const exampleSearchResults: DiscoverHit[] = [
  {
    document: { id: 'doc-001', title: 'System Architecture Overview', lifecycle_status: 'active', source_type: 'docx', version_label: 'v2', project: 'example_vault', doc_type: 'technical_disclosure', tags: ['architecture'] },
    chunk_content: 'The example platform is a modular system designed around three core principles: data sovereignty, contextual awareness, and adaptive learning. Each module communicates through well-defined API boundaries.',
    heading_path: '1. Introduction',
    relevance_score: 0.94,
  },
  {
    document: { id: 'doc-003', title: 'CD-04 Interaction Context Design Spec', lifecycle_status: 'active', source_type: 'docx', version_label: 'v1', project: 'example_vault', doc_type: 'design_spec', tags: ['design'] },
    chunk_content: 'A computer-implemented method for capturing and structuring contextual data from patient-provider interactions, wherein the system maintains a persistent context graph that evolves with each encounter.',
    heading_path: 'Claims > Claim 1',
    relevance_score: 0.87,
  },
  {
    document: { id: 'doc-004', title: 'Integration Catalog', lifecycle_status: 'active', source_type: 'markdown', version_label: null, project: 'example_vault', doc_type: 'reference', tags: ['integration'] },
    chunk_content: 'EHR Integration uses HL7 FHIR R4 resources. The platform acts as a FHIR client, querying patient records via the standard REST API and mapping responses to the internal data model.',
    heading_path: 'EHR Integration',
    relevance_score: 0.72,
  },
  {
    document: { id: 'doc-008', title: 'Glossary', lifecycle_status: 'active', source_type: 'markdown', version_label: null, project: 'example_vault', doc_type: 'reference', tags: ['glossary'] },
    chunk_content: 'Adaptive Learning Engine - The subsystem responsible for adjusting recommendation models based on observed provider behavior and patient outcome data.',
    heading_path: 'A',
    relevance_score: 0.65,
  },
];

const exampleScanResults: ScanResultItem[] = [
  {
    file_path: '/path/to/example_inbox/CD-05_ClinicalPathway_v1.docx',
    file_hash: 'sha256:e3b0c44298fc1c149afbf4c8996fb924',
    source_modified_at: '2026-04-05T14:20:00Z',
    source_type: 'docx',
    parsed_metadata: { title: 'ClinicalPathway', date: null, project: null, codes: ['CD-05'], version: 'v1', doc_type: 'design_spec' },
    sage_status: 'new',
  },
  {
    file_path: '/path/to/example_inbox/PIM_Architecture_Overview_v3.docx',
    file_hash: 'sha256:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
    source_modified_at: '2026-04-06T09:15:00Z',
    source_type: 'docx',
    parsed_metadata: { title: 'Architecture Overview', date: null, project: 'example', codes: [], version: 'v3', doc_type: 'technical_disclosure' },
    sage_status: 'modified',
  },
  {
    file_path: '/path/to/example_inbox/PIM_Integration_Catalog.md',
    file_hash: 'sha256:intcat789000111222333444555666777',
    source_modified_at: '2026-03-09T17:00:00Z',
    source_type: 'markdown',
    parsed_metadata: { title: 'Integration Catalog', date: null, project: 'example', codes: [], version: null, doc_type: 'reference' },
    sage_status: 'unchanged',
  },
  {
    file_path: '/path/to/example_inbox/meeting_recording.mp4',
    file_hash: 'sha256:mp4hash000111222333444555666777888',
    source_modified_at: '2026-04-02T16:00:00Z',
    source_type: null,
    parsed_metadata: { title: 'meeting_recording', date: null, project: null, codes: [], version: null, doc_type: null },
    sage_status: 'no_adapter',
  },
  {
    file_path: '/path/to/example_inbox/Q2_Planning_Notes.md',
    file_hash: 'sha256:q2plan000111222333444555666777888',
    source_modified_at: '2026-04-01T10:30:00Z',
    source_type: 'markdown',
    parsed_metadata: { title: 'Q2 Planning Notes', date: null, project: null, codes: [], version: null, doc_type: 'meeting_notes' },
    sage_status: 'new',
  },
  {
    file_path: '/path/to/example_inbox/Prior_Art_Analysis.pdf',
    file_hash: 'sha256:priorart000111222333444555666777',
    source_modified_at: '2026-03-28T08:45:00Z',
    source_type: 'pdf',
    parsed_metadata: { title: 'Prior Art Analysis', date: null, project: null, codes: [], version: null, doc_type: 'reference' },
    sage_status: 'new',
  },
];

export const mockIngestSummary: IngestSummaryEvent = {
  event_type: 'summary',
  documents_created: { new: 2, new_version: 1 },
  metadata_pending: 3,
  edges_created: { supersedes: 1 },
  edges_staged: { covers: 2 },
  edges_dropped: 0,
  abstracts_generated: 2,
  abstracts_deferred: 1,
  error_count: 0,
};

// --- Vault assembly ---

export const vaults: Record<string, VaultData> = {
  example_vault: {
    identity: {
      id: 'example_vault',
      name: 'Example Vault',
      owner: 'clif',
      description: 'Example portfolio of design specs and technical documentation.',
      storage_root: '/path/to/example_vault',
      brain_root: '/path/to/sage_vaults/example_vault',
    },
    doc_types: [
      { value: 'design_spec', label: 'Design Spec' },
      { value: 'technical_disclosure', label: 'Technical Disclosure' },
      { value: 'reference', label: 'Reference Document' },
      { value: 'status_report', label: 'Status Report' },
      { value: 'meeting_notes', label: 'Meeting Notes' },
    ],
    lifecycle_states: [
      { value: 'draft', label: 'Draft', is_terminal: false },
      { value: 'active', label: 'Active', is_terminal: false },
      { value: 'completed', label: 'Completed', is_terminal: false },
      { value: 'archived', label: 'Archived', is_terminal: true },
    ],
    adapters: [
      { source_type: 'markdown', enabled: true, extensions: ['.md'], version: '1.0.0' },
      { source_type: 'docx', enabled: true, extensions: ['.docx'], version: '1.0.0' },
      { source_type: 'pdf', enabled: true, extensions: ['.pdf'], version: '1.0.0' },
    ],
    documents: exampleDocuments,
    edges: exampleEdges,
    staging_edges: exampleStagingEdges,
    pending_metadata: examplePendingMetadata,
    search_results: exampleSearchResults,
    mock_scan_results: exampleScanResults,
  },
  personal_notes: {
    identity: {
      id: 'personal_notes',
      name: 'Personal Notes',
      owner: 'clif',
      description: 'Personal knowledge base and research notes.',
      storage_root: '/path/to/notes',
      brain_root: '/path/to/sage_vaults/personal_notes',
    },
    doc_types: [
      { value: 'note', label: 'Note' },
      { value: 'article', label: 'Article' },
      { value: 'bookmark', label: 'Bookmark' },
    ],
    lifecycle_states: [
      { value: 'draft', label: 'Draft', is_terminal: false },
      { value: 'active', label: 'Active', is_terminal: false },
      { value: 'archived', label: 'Archived', is_terminal: true },
    ],
    adapters: [
      { source_type: 'markdown', enabled: true, extensions: ['.md'], version: '1.0.0' },
    ],
    documents: [],
    edges: [],
    staging_edges: [],
    pending_metadata: [],
    search_results: [],
    mock_scan_results: [],
  },
};

export const defaultVaultId = 'example_vault';

// --- Helper functions ---

export function getDocument(vaultId: string, docId: string): Document | undefined {
  return vaults[vaultId]?.documents.find(d => d.id === docId);
}

export function getDocumentTitle(vaultId: string, docId: string): string {
  return getDocument(vaultId, docId)?.title ?? docId;
}

export function getEdgesForDocument(vaultId: string, docId: string): Edge[] {
  return vaults[vaultId]?.edges.filter(e => e.source_id === docId || e.target_id === docId) ?? [];
}

export function getVaultStats(vaultId: string) {
  const vault = vaults[vaultId];
  if (!vault) return null;
  const docs = vault.documents;
  const edges = vault.edges;

  const byLifecycle: Record<string, number> = {};
  const byDocType: Record<string, number> = {};
  const byAdapter: Record<string, number> = {};

  for (const d of docs) {
    byLifecycle[d.lifecycle_status] = (byLifecycle[d.lifecycle_status] ?? 0) + 1;
    if (d.doc_type) byDocType[d.doc_type] = (byDocType[d.doc_type] ?? 0) + 1;
    byAdapter[d.source_type] = (byAdapter[d.source_type] ?? 0) + 1;
  }

  const byEdgeType: Record<string, number> = {};
  for (const e of edges) {
    byEdgeType[e.edge_type] = (byEdgeType[e.edge_type] ?? 0) + 1;
  }

  return {
    totalDocuments: docs.length,
    byLifecycle,
    byDocType,
    byAdapter,
    totalEdges: edges.length,
    byEdgeType,
    stagingEdgeCount: vault.staging_edges.length,
    lancedbSize: '12.4 MB',
    sqliteSize: '840 KB',
    lastIngestion: '2026-04-02T15:01:00Z',
    pendingMetadata: vault.pending_metadata.length,
    pendingEdges: vault.staging_edges.length,
    deferredAbstracts: docs.filter(d => d.pipeline_status === 'abstraction_skipped').length,
    failedIngestions: docs.filter(d => d.pipeline_status === 'failed'),
  };
}
