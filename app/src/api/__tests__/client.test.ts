import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { apiUploadStream, ApiError } from '../client';

const originalFetch = globalThis.fetch;

describe('apiUploadStream', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('C1: POSTs the FormData with no Content-Type header and returns the body stream', async () => {
    const resp = new Response('event: progress\ndata: {}\n\n', { status: 200 });
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(resp);

    const form = new FormData();
    form.append('files', new File(['x'], 'a.md'));
    form.append('metadata', '{}');

    const stream = await apiUploadStream('/sage_vaults/v1/documents:batch', form);

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/sage_vaults/v1/documents:batch');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(form);
    // The browser must set the multipart boundary itself: a hand-set
    // Content-Type corrupts the boundary and the server fails to parse.
    expect(init.headers).toBeUndefined();
    expect(stream).toBe(resp.body);
  });

  it('C2: throws ApiError carrying the server code on a non-ok JSON error', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(
        JSON.stringify({ code: 'invalid_batch_metadata', message: 'files length mismatch' }),
        { status: 400, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const form = new FormData();
    form.append('metadata', '{}');

    await expect(apiUploadStream('/sage_vaults/v1/documents:batch', form)).rejects.toMatchObject({
      name: 'ApiError',
      code: 'invalid_batch_metadata',
    });
  });

  it('C2b: exposes ApiError.code', () => {
    expect(new ApiError('invalid_batch_metadata', 'x').code).toBe('invalid_batch_metadata');
  });
});
