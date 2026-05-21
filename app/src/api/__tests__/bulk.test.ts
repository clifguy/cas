import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { bulkSetLifecycle, bulkUpdateMetadata } from '../bulk';
import { ApiError } from '../client';
import type { BulkMetadataItem } from '../types';

const originalFetch = globalThis.fetch;

function mockJsonResponse(body: unknown, init: ResponseInit = { status: 200 }): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  });
}

describe('bulkSetLifecycle', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('posts to /sage_vaults/{vaultId}/lifecycle/bulk with the items payload', async () => {
    const okBody = { results: [], success_count: 0, error_count: 0, total: 0 };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(mockJsonResponse(okBody));

    const items = [
      { document_id: 'A', action: 'archive' },
      { document_id: 'B', action: 'archive' },
    ];
    await bulkSetLifecycle('test_vault', items);

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/sage_vaults/test_vault/lifecycle/bulk');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ items });
  });

  it('propagates an ApiError on 4xx vault-level failure', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      mockJsonResponse(
        { code: 'vault_not_found', message: 'no vault registered with that id' },
        { status: 404 },
      ),
    );

    await expect(bulkSetLifecycle('ghost_vault', [{ document_id: 'A', action: 'archive' }])).rejects.toMatchObject({
      name: 'ApiError',
      code: 'vault_not_found',
    });
  });
});

describe('bulkUpdateMetadata', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('posts to /sage_vaults/{vaultId}/metadata/bulk with the items payload', async () => {
    const okBody = { results: [], success_count: 0, error_count: 0, total: 0 };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(mockJsonResponse(okBody));

    const items = [{ document_id: 'A', tags: { add: ['foo'] } }];
    await bulkUpdateMetadata('test_vault', items);

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/sage_vaults/test_vault/metadata/bulk');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ items });
  });

  it('round-trips all six optional scalar fields per item (T-0142)', async () => {
    const okBody = { results: [], success_count: 0, error_count: 0, total: 0 };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(mockJsonResponse(okBody));

    const fullItem: BulkMetadataItem = {
      document_id: 'D1',
      title: 'New title',
      version_label: 'v2',
      project: 'CAS',
      tags: { add: ['x'] },
      doc_type: 'note',
      authority_scope: 'internal',
      document_date: '2026-05-21',
      tier3_metadata: { set: { foo: 'bar' } },
    };
    await bulkUpdateMetadata('test_vault', [fullItem]);

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/sage_vaults/test_vault/metadata/bulk');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ items: [fullItem] });
  });
});

describe('ApiError contract', () => {
  it('exposes the .code property the tests rely on', () => {
    const err = new ApiError('vault_not_found', 'no vault registered with that id');
    expect(err.code).toBe('vault_not_found');
  });
});
