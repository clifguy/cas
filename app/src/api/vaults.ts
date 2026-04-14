import { apiGet, apiPost, apiPut } from './client';
import type { VaultSummary, VaultStats, VaultConfig, UpdateConfigResponse } from './types';

export async function listVaults(): Promise<VaultSummary[]> {
  return apiGet<VaultSummary[]>('/sage_vaults');
}

export async function getVaultStats(vaultId: string): Promise<VaultStats> {
  return apiGet<VaultStats>(`/sage_vaults/${vaultId}/stats`);
}

export async function getVaultConfig(vaultId: string): Promise<VaultConfig> {
  return apiGet<VaultConfig>(`/sage_vaults/${vaultId}/config`);
}

export async function updateVaultConfig(
  vaultId: string,
  sections: Partial<VaultConfig>,
): Promise<UpdateConfigResponse> {
  return apiPut<UpdateConfigResponse>(`/sage_vaults/${vaultId}/config`, sections);
}

export async function createVault(config: Record<string, unknown>): Promise<VaultSummary> {
  return apiPost<VaultSummary>('/sage_vaults', { config });
}
