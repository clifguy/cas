import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../client';
import { getDefaultVaultConfig } from '../vaults';
import type { DefaultVaultConfig } from '../types';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return { ...actual, apiGet: vi.fn() };
});

const apiGetMock = vi.mocked(client.apiGet);

describe('getDefaultVaultConfig', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('issues GET to the default-config path with the vault id as a query parameter', async () => {
    const served = { vault: { id: 'my_vault' } } as unknown as DefaultVaultConfig;
    apiGetMock.mockResolvedValue(served);

    const result = await getDefaultVaultConfig('my_vault');

    expect(apiGetMock).toHaveBeenCalledWith('/sage_vaults/default-config?vault_id=my_vault');
    expect(result).toBe(served);
  });

  it('encodes the vault id rather than splicing it into the query raw', async () => {
    apiGetMock.mockResolvedValue({} as DefaultVaultConfig);

    await getDefaultVaultConfig('a&b=c');

    expect(apiGetMock).toHaveBeenCalledWith('/sage_vaults/default-config?vault_id=a%26b%3Dc');
  });
});
