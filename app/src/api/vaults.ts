import { apiGet } from './client';
import type { VaultSummary, VaultStats } from './types';

export async function listVaults(): Promise<VaultSummary[]> {
  return apiGet<VaultSummary[]>('/sage_vaults');
}

export async function getVaultStats(vaultId: string): Promise<VaultStats> {
  return apiGet<VaultStats>(`/sage_vaults/${vaultId}/stats`);
}
