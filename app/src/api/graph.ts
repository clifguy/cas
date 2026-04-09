import { apiPost } from './client';
import type { TraverseRequest, TraverseResponse, LinkRequest, Edge } from './types';

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
  return apiPost<Edge>(`/sage_vaults/${vaultId}/edges`, request);
}
