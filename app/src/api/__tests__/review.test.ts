import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../client';
import { listPendingMetadata } from '../review';
import type { PendingMetadataPage } from '../types';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return { ...actual, apiGet: vi.fn() };
});

const apiGetMock = vi.mocked(client.apiGet);

describe('listPendingMetadata', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('asks for the largest page of full rows and returns the page', async () => {
    const served: PendingMetadataPage = {
      items: [],
      total_available: 0,
      limit: 100,
      offset: 0,
      response_mode: 'full',
    };
    apiGetMock.mockResolvedValue(served);

    const result = await listPendingMetadata('my_vault');

    expect(apiGetMock).toHaveBeenCalledWith(
      '/sage_vaults/my_vault/pending-metadata?limit=100&response_mode=full',
    );
    expect(result).toBe(served);
  });
});
