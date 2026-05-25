import { apiPost, apiStream, readSSEStream } from './client';
import type {
  ScanResponse,
  ParsedMetadataItem,
  IngestProgressEvent,
  IngestSummaryEvent,
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
