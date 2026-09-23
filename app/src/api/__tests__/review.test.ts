import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../client';
import { listPendingMetadata, listStagingEdges } from '../review';
import type {
  PendingMetadata,
  PendingMetadataPage,
  StagingEdge,
  StagingEdgeListResponse,
} from '../types';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return { ...actual, apiGet: vi.fn() };
});

const apiGetMock = vi.mocked(client.apiGet);

function page(ids: string[], offset: number, total: number): PendingMetadataPage {
  return {
    items: ids.map((id) => ({ document: { id } }) as unknown as PendingMetadata),
    total_available: total,
    limit: 100,
    offset,
    response_mode: 'full',
  };
}

function ids(prefix: string, count: number): string[] {
  return Array.from({ length: count }, (_, i) => `${prefix}${i}`);
}

describe('listPendingMetadata', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('asks for full rows and returns a single page whole', async () => {
    apiGetMock.mockResolvedValue(page(['a', 'b'], 0, 2));

    const result = await listPendingMetadata('my_vault');

    expect(apiGetMock).toHaveBeenCalledTimes(1);
    expect(apiGetMock).toHaveBeenCalledWith(
      '/sage_vaults/my_vault/pending-metadata?limit=100&offset=0&response_mode=full',
    );
    expect(result.items.map((i) => i.document.id)).toEqual(['a', 'b']);
  });

  it('reads every page, so a queue longer than one page is not truncated', async () => {
    apiGetMock
      .mockResolvedValueOnce(page(ids('d', 100), 0, 150))
      .mockResolvedValueOnce(page(ids('e', 50), 100, 150));

    const result = await listPendingMetadata('my_vault');

    expect(apiGetMock).toHaveBeenNthCalledWith(
      2,
      '/sage_vaults/my_vault/pending-metadata?limit=100&offset=100&response_mode=full',
    );
    expect(result.items).toHaveLength(150);
    expect(result.items[149].document.id).toBe('e49');
    expect(result.total_available).toBe(150);
  });

  it('stops on an empty page even when the total says more remain', async () => {
    apiGetMock
      .mockResolvedValueOnce(page(['a'], 0, 5))
      .mockResolvedValueOnce(page([], 100, 5));

    const result = await listPendingMetadata('my_vault');

    expect(apiGetMock).toHaveBeenCalledTimes(2);
    expect(result.items).toHaveLength(1);
  });
});

describe('listStagingEdges', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
  });

  it('returns the edges carried in the list envelope', async () => {
    const items = [{ id: 'e1' }] as unknown as StagingEdge[];
    const served: StagingEdgeListResponse = {
      items,
      count: 1,
      read_meta: { success: true, body_present: false, server_build: '1.0.0+abc' },
    };
    apiGetMock.mockResolvedValue(served);

    const result = await listStagingEdges('my_vault');

    expect(apiGetMock).toHaveBeenCalledWith('/sage_vaults/my_vault/staging-edges');
    expect(result).toBe(items);
  });
});
