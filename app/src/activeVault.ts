import type { VaultSummary } from './api/types';

export const VAULT_STORAGE_KEY = 'cas.activeVault';

export function resolveInitialVaultId(
  vaults: VaultSummary[],
  persistedId: string | null,
): string {
  if (persistedId && vaults.some(v => v.id === persistedId)) return persistedId;
  return vaults[0]?.id ?? '';
}
