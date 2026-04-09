import { apiPost } from './client';
import type { DiscoverRequest, DiscoverResponse } from './types';

export async function discover(
  vaultId: string,
  request: DiscoverRequest,
): Promise<DiscoverResponse> {
  return apiPost<DiscoverResponse>(`/sage_vaults/${vaultId}/discover`, request);
}
