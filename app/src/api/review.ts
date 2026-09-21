import { apiGet, apiPost } from './client';
import type {
  PendingMetadataPage,
  StagingEdge,
  StagingEdgeConfirmResponse,
  StagingEdgeDismissResponse,
} from './types';

// The largest page the queue serves, as full rows: the review table needs each
// document's extracted-field annotations, which the light projection omits.
export async function listPendingMetadata(vaultId: string): Promise<PendingMetadataPage> {
  return apiGet<PendingMetadataPage>(
    `/sage_vaults/${vaultId}/pending-metadata?limit=100&response_mode=full`,
  );
}

export async function listStagingEdges(vaultId: string): Promise<StagingEdge[]> {
  return apiGet<StagingEdge[]>(`/sage_vaults/${vaultId}/staging-edges`);
}

export async function confirmStagingEdge(
  vaultId: string,
  edgeId: string,
): Promise<StagingEdgeConfirmResponse> {
  return apiPost<StagingEdgeConfirmResponse>(`/sage_vaults/${vaultId}/staging-edges/${edgeId}/confirm`, {});
}

export async function dismissStagingEdge(
  vaultId: string,
  edgeId: string,
): Promise<StagingEdgeDismissResponse> {
  return apiPost<StagingEdgeDismissResponse>(`/sage_vaults/${vaultId}/staging-edges/${edgeId}/dismiss`, {});
}
