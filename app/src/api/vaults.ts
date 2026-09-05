import { apiGet, apiPost, apiPut } from './client';
import type {
  VaultSummary,
  VaultStats,
  VaultConfig,
  DefaultVaultConfig,
  UpdateConfigResponse,
} from './types';

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

// The scaffold a vault with this id would be created with. Served rather
// than assembled here: the server derives the storage and brain roots, and
// a second copy of the rest drifts from the first.
export async function getDefaultVaultConfig(vaultId: string): Promise<DefaultVaultConfig> {
  return apiGet<DefaultVaultConfig>(
    `/sage_vaults/default-config?vault_id=${encodeURIComponent(vaultId)}`,
  );
}

export async function createVault(config: Record<string, unknown>): Promise<VaultSummary> {
  return apiPost<VaultSummary>('/sage_vaults', { config });
}
