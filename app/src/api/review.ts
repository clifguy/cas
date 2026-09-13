import { apiGet, apiPost } from './client';
import type {
  PendingMetadata,
  StagingEdge,
  StagingEdgeConfirmResponse,
  StagingEdgeDismissResponse,
} from './types';

export async function listPendingMetadata(vaultId: string): Promise<PendingMetadata[]> {
  return apiGet<PendingMetadata[]>(`/sage_vaults/${vaultId}/pending-metadata`);
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
