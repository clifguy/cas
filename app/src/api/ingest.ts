import { ApiError, apiPost, apiStream, apiUploadStream, readSSEStream } from './client';
import type {
  ScanResponse,
  ParsedMetadataItem,
  IngestProgressEvent,
  IngestSummaryEvent,
  BatchIngestUploadMetadata,
  BatchIngestEvent,
} from './types';

export async function scanDirectory(
  vaultId: string,
  directory: string,
  maxDepth?: number,
): Promise<ScanResponse> {
  return apiPost<ScanResponse>('/app/scan', {
    vault_id: vaultId,
    directory,
    max_depth: maxDepth ?? null,
  });
}

export interface IngestFileItem {
  file_path: string;
  source_type: string;
  parsed_metadata?: ParsedMetadataItem;
}

export type IngestEvent = IngestProgressEvent | IngestSummaryEvent;

function isIngestEvent(data: Record<string, unknown>): boolean {
  return data.event_type === 'progress' || data.event_type === 'summary';
}

/**
 * Start a batch ingestion via SSE stream. Calls onEvent for each progress
 * and summary event. Returns a promise that resolves when the stream ends.
 */
export async function startIngestion(
  vaultId: string,
  files: IngestFileItem[],
  onEvent: (event: IngestEvent) => void,
  signal?: AbortSignal,
  inferEdges: boolean = false,
): Promise<void> {
  const stream = await apiStream('/app/ingest', {
    vault_id: vaultId,
    files,
    infer_edges: inferEdges,
  }, signal);

  await readSSEStream(stream, (data) => {
    if (isIngestEvent(data)) {
      onEvent(data as unknown as IngestEvent);
    } else {
      console.warn('[ingest] Unexpected SSE event:', data);
    }
  });
}

// --- Cloud upload bulk-ingest (SAGE documents:batch) ---

/**
 * Map a filename to its SAGE source_type by extension, or null when no enabled
 * adapter handles it. The closed SourceType vocabulary is markdown / docx /
 * xlsx / pptx / pdf; the browser must supply source_type per file because, unlike the
 * directory scan, there is no server-side adapter detection on an upload.
 */
export function sourceTypeForFilename(name: string): string | null {
  const dot = name.lastIndexOf('.');
  const ext = dot === -1 ? '' : name.slice(dot + 1).toLowerCase();
  switch (ext) {
    case 'md':
    case 'markdown':
      return 'markdown';
    case 'docx':
      return 'docx';
    case 'xlsx':
      return 'xlsx';
    case 'pptx':
      return 'pptx';
    case 'pdf':
      return 'pdf';
    default:
      return null;
  }
}

export interface UploadFileItem {
  file: File;
  source_type: string;
}

export interface UploadBatchOptions {
  inferEdges?: boolean;
  needsReview?: boolean;
}

/**
 * Upload file content to the SAGE batch-ingest endpoint as multipart/form-data
 * and stream the per-file progress and final summary back via SSE. The cloud
 * (hosted) analogue of startIngestion: there the browser holds the files and
 * the server shares no filesystem, so content is delivered by upload rather
 * than by path. The `metadata.files` array aligns by position with the uploaded
 * `files` parts. Calls onEvent for each progress and summary event; resolves
 * when the stream ends.
 */
export async function uploadBatchIngest(
  vaultId: string,
  items: UploadFileItem[],
  onEvent: (event: BatchIngestEvent) => void,
  signal?: AbortSignal,
  { inferEdges = false, needsReview = true }: UploadBatchOptions = {},
): Promise<void> {
  const metadata: BatchIngestUploadMetadata = {
    infer_edges: inferEdges,
    needs_review: needsReview,
    files: items.map((item) => ({ source_type: item.source_type })),
  };

  const form = new FormData();
  for (const item of items) {
    form.append('files', item.file, item.file.name);
  }
  form.append('metadata', JSON.stringify(metadata));

  const stream = await apiUploadStream(
    `/sage_vaults/${vaultId}/documents:batch`,
    form,
    signal,
  );

  await readSSEStream(stream, (data) => {
    if (isIngestEvent(data)) {
      onEvent(data as unknown as BatchIngestEvent);
    } else {
      console.warn('[ingest] Unexpected batch SSE event:', data);
    }
  });
}

// --- Profile detection ---

export type IngestProfile = 'hosted' | 'co-located';

/**
 * A directory path that cannot exist, used only to probe the deployment
 * profile. In the hosted profile /app/scan reports local_profile_only before
 * touching the filesystem; in the co-located profile it rejects this path as
 * invalid_directory after an existence check, walking nothing.
 */
export const PROFILE_PROBE_PATH = '/__cas_ingest_profile_probe__';

/**
 * Decide which bulk-ingest affordance the Ingest view should render. A
 * `?profile=hosted|colocated` (or `co-located`) URL override wins when present
 * (used by the browser e2e and for manual smoke-testing); otherwise probe
 * /app/scan with a non-existent sentinel path and key on the local_profile_only
 * signal. The co-located profile keeps the directory-path scan; the hosted
 * profile shows the upload affordance.
 */
export async function detectIngestProfile(vaultId: string): Promise<IngestProfile> {
  const override = new URLSearchParams(window.location.search).get('profile');
  if (override === 'hosted') return 'hosted';
  if (override === 'colocated' || override === 'co-located') return 'co-located';

  try {
    await apiPost('/app/scan', {
      vault_id: vaultId,
      directory: PROFILE_PROBE_PATH,
      max_depth: null,
    });
    return 'co-located';
  } catch (err) {
    if (err instanceof ApiError && err.code === 'local_profile_only') {
      return 'hosted';
    }
    return 'co-located';
  }
}
