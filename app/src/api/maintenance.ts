// Typed client wrappers for the SAGE Core API maintenance surface (T-0117).
//
// startReabstract — SSE-streamed POST against
//   /sage_vaults/{vault_id}/admin/reabstract-deferred (T-0134). Shape mirrors
//   startIngestion in app/src/api/ingest.ts: caller passes an onEvent
//   callback that is invoked for each parsed progress / summary event.
// getDeferredCount — catalog discover filtered by
//   pipeline_status='abstraction_skipped'; returns total_available so the
//   Maintenance panel can size the worklist without fetching rows.

import { apiPost, apiStream, readSSEStream } from './client';
import type {
  DiscoverResponse,
  OptimizeContentStoreReport,
  ReabstractEvent,
} from './types';

function isReabstractEvent(data: Record<string, unknown>): boolean {
  return data.event_type === 'progress' || data.event_type === 'summary';
}

/**
 * Start a reabstract-deferred run via SSE stream. Calls onEvent for each
 * 'progress' (per-document) and 'summary' (final) event. Resolves when the
 * stream ends. Rejects with ApiError before opening the stream if the route
 * returns 404 (vault_not_found) or 409 (reabstract_already_in_flight).
 */
export async function startReabstract(
  vaultId: string,
  onEvent: (event: ReabstractEvent) => void,
  signal?: AbortSignal,
  includePdf: boolean = false,
): Promise<void> {
  const stream = await apiStream(
    `/sage_vaults/${vaultId}/admin/reabstract-deferred`,
    { include_pdf: includePdf },
    signal,
  );

  await readSSEStream(stream, (data) => {
    if (isReabstractEvent(data)) {
      onEvent(data as unknown as ReabstractEvent);
    } else {
      console.warn('[reabstract] Unexpected SSE event:', data);
    }
  });
}

/**
 * Count deferred-abstract documents in a vault. Uses the existing discover
 * surface with a `pipeline_status='abstraction_skipped'` filter and reads
 * total_available, so no SAGE-side endpoint is needed.
 */
export async function getDeferredCount(vaultId: string): Promise<number> {
  const resp = await apiPost<DiscoverResponse>(
    `/sage_vaults/${vaultId}/discover`,
    {
      mode: 'catalog',
      filters: { pipeline_status: 'abstraction_skipped' },
      limit: 1,
    },
  );
  return resp.total_available;
}

/**
 * Compact the vault's vector database content store. Synchronous JSON POST:
 * the backend optimize call blocks until the store returns, then sends the
 * pre/post snapshot back in one shot. `cleanupOlderThanDays` defaults to 7
 * (the embedded backend's own default); 0 prunes every version except the
 * latest.
 */
export async function startOptimizeContentStore(
  vaultId: string,
  cleanupOlderThanDays: number,
): Promise<OptimizeContentStoreReport> {
  return apiPost<OptimizeContentStoreReport>(
    `/sage_vaults/${vaultId}/admin/optimize-content-store`,
    { cleanup_older_than_days: cleanupOlderThanDays },
  );
}
