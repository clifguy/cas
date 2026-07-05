import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../client';
import { getDocumentDownloadUrl, documentContentUrl } from '../documents';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return {
    ...actual,
    apiGet: vi.fn(),
    apiPost: vi.fn(),
  };
});

const apiGetMock = vi.mocked(client.apiGet);

describe('getDocumentDownloadUrl', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('issues GET to the download-url path and returns the response', async () => {
    apiGetMock.mockResolvedValue({ download_url: 'https://sp.example/dl?t=xyz' });

    const result = await getDocumentDownloadUrl('test_vault', 'doc-42');

    expect(apiGetMock).toHaveBeenCalledWith(
      '/sage_vaults/test_vault/documents/doc-42/download-url',
    );
    expect(result).toEqual({ download_url: 'https://sp.example/dl?t=xyz' });
  });
});

describe('documentContentUrl', () => {
  it('builds the same-origin streaming content-route path', () => {
    expect(documentContentUrl('test_vault', 'doc-42')).toBe(
      '/sage_vaults/test_vault/documents/doc-42/content',
    );
  });
});
