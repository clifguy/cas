import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', async () => {
  const actual = await vi.importActual<typeof import('./client')>('./client');
  return {
    ...actual,
    apiGet: vi.fn(),
    apiPost: vi.fn(),
  };
});

import * as client from './client';
import { reabstractDocument } from './documents';

const apiPostMock = vi.mocked(client.apiPost);

describe('reabstractDocument', () => {
  beforeEach(() => {
    apiPostMock.mockReset();
  });

  it('POSTs to the per-document reabstract route and returns the started envelope', async () => {
    apiPostMock.mockResolvedValue({
      status: 'reabstract_started',
      document_id: 'deadbeef_doc',
      dispatched_at: '2026-07-04T00:00:00Z',
    });

    const result = await reabstractDocument('test_vault', 'deadbeef_doc');

    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const [path, body] = apiPostMock.mock.calls[0];
    expect(path).toBe('/sage_vaults/test_vault/documents/deadbeef_doc/reabstract');
    // The route takes vault_id and document_id from the path; no request body.
    expect(body).toEqual({});
    expect(result).toEqual({
      status: 'reabstract_started',
      document_id: 'deadbeef_doc',
      dispatched_at: '2026-07-04T00:00:00Z',
    });
  });
});
