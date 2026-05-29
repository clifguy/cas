import { apiPost } from './client';
import type {
  BulkLinkItemResult,
  BulkLinkResponse,
  Edge,
  LinkRequest,
  TraverseRequest,
  TraverseResponse,
} from './types';

export async function traverse(
  vaultId: string,
  request: TraverseRequest,
): Promise<TraverseResponse> {
  return apiPost<TraverseResponse>(`/sage_vaults/${vaultId}/traverse`, request);
}

export async function createEdge(
  vaultId: string,
  request: LinkRequest,
): Promise<Edge> {
  // CAS-ADR-029 v4 plural-noun: single endpoint POST /sage_vaults/{id}/edges
  // takes an items array; the singleton-shaped caller signature is preserved
  // by wrapping the request as a length-1 items collection and unwrapping
  // the per-item result envelope.
  const response = await apiPost<BulkLinkResponse>(
    `/sage_vaults/${vaultId}/edges`,
    { items: [request] },
  );
  const item: BulkLinkItemResult | undefined = response.results[0];
  if (!item || item.status !== 'success' || !item.edge) {
    const err = item?.error;
    throw new Error(err ? `${err.error}: ${err.message}` : 'create_edge failed');
  }
  return item.edge;
}
