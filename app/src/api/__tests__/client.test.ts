import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import {
  apiUploadStream,
  apiGet,
  apiPostVoid,
  onAuthRequired,
  ApiError,
} from '../client';

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

describe('apiPostVoid', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('B1: resolves on a 204 without reading the (absent) body', async () => {
    const resp = new Response(null, { status: 204 });
    const jsonSpy = vi.spyOn(resp, 'json');
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(resp);

    await expect(apiPostVoid('/app/auth/logout')).resolves.toBeUndefined();

    // The endpoint returns 204; parsing its empty body would throw, which is
    // exactly why apiPost cannot be used here.
    expect(jsonSpy).not.toHaveBeenCalled();
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/app/auth/logout');
    expect(init.method).toBe('POST');
  });

  it('B2: throws ApiError carrying the server code on a non-ok response', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ code: 'bad', message: 'nope' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiPostVoid('/app/auth/logout')).rejects.toMatchObject({
      name: 'ApiError',
      code: 'bad',
    });
  });
});

describe('onAuthRequired signal', () => {
  let unsubscribe: (() => void) | undefined;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    unsubscribe?.();
    unsubscribe = undefined;
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('B3: a 401 auth_required notifies subscribers and still throws', async () => {
    const listener = vi.fn();
    unsubscribe = onAuthRequired(listener);
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ code: 'auth_required', message: 'expired' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiGet('/sage_vaults')).rejects.toMatchObject({ code: 'auth_required' });
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('B4: a non-auth_required error does not notify subscribers', async () => {
    const listener = vi.fn();
    unsubscribe = onAuthRequired(listener);
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ code: 'forbidden', message: 'no' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiGet('/sage_vaults')).rejects.toMatchObject({ code: 'forbidden' });
    expect(listener).not.toHaveBeenCalled();
  });

  it('B5: an unsubscribed listener is not notified', async () => {
    const listener = vi.fn();
    onAuthRequired(listener)();
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ code: 'auth_required', message: 'expired' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiGet('/sage_vaults')).rejects.toMatchObject({ code: 'auth_required' });
    expect(listener).not.toHaveBeenCalled();
  });
});

describe('errorFromResponse message fallback', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  // The HTTP/2 edge carries no reason-phrase, so response.statusText is the empty
  // string; a non-JSON 5xx body leaves the parsed body undefined. Together these
  // must still yield a non-empty, human-meaningful message (a bare empty message
  // reads as "no error" in the views and blanks the panel).
  it('T1: yields an HTTP-status message when statusText is empty and the body is non-JSON', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('Internal Server Error', { status: 500, statusText: '' }),
    );

    await expect(apiGet('/sage_vaults/v1/stats')).rejects.toThrow('HTTP 500');
  });

  it('T2: carries the HTTP_<status> code and a non-empty message for a non-JSON 5xx', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('boom', { status: 503, statusText: '' }),
    );

    const err = await apiGet('/x').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('HTTP_503');
    expect((err as ApiError).message.length).toBeGreaterThan(0);
  });

  it('T3: prefers the server-provided message when the error body is JSON', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(JSON.stringify({ code: 'x', message: 'specific detail' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(apiGet('/x')).rejects.toThrow('specific detail');
  });

  it('T4: preserves the reason-phrase when present (HTTP/1.1 local dev)', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('boom', { status: 500, statusText: 'Internal Server Error' }),
    );

    await expect(apiGet('/x')).rejects.toThrow('Internal Server Error');
  });
});
