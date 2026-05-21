// Vitest specs for the maintenance API client wrappers (T-0117).
//
// Strategy: mock the shared low-level helpers in ./client so the tests
// exercise the wrapper logic (URL composition, body shape, event forwarding,
// error propagation) without depending on jsdom's SSE stream plumbing.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiError } from './client';
import * as client from './client';
import { startReabstract, getDeferredCount } from './maintenance';
import type { ReabstractEvent } from './types';

vi.mock('./client', async () => {
  const actual = await vi.importActual<typeof import('./client')>('./client');
  return {
    ...actual,
    apiStream: vi.fn(),
    readSSEStream: vi.fn(),
    apiPost: vi.fn(),
  };
});

const apiStreamMock = vi.mocked(client.apiStream);
const readSSEStreamMock = vi.mocked(client.readSSEStream);
const apiPostMock = vi.mocked(client.apiPost);

describe('startReabstract', () => {
  beforeEach(() => {
    apiStreamMock.mockReset();
    readSSEStreamMock.mockReset();
  });

  it('A1: opens the SSE stream against the correct URL with default body', async () => {
    apiStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockResolvedValue();

    await startReabstract('v1', vi.fn());

    expect(apiStreamMock).toHaveBeenCalledTimes(1);
    const [path, body] = apiStreamMock.mock.calls[0];
    expect(path).toBe('/sage_vaults/v1/admin/reabstract-deferred');
    expect(body).toEqual({ include_pdf: false });
  });

  it('A1b: honors includePdf=true in the request body', async () => {
    apiStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockResolvedValue();

    await startReabstract('v1', vi.fn(), undefined, true);

    const [, body] = apiStreamMock.mock.calls[0];
    expect(body).toEqual({ include_pdf: true });
  });

  it('A2: forwards each parsed event to onEvent in stream order', async () => {
    const events: ReabstractEvent[] = [
      {
        event_type: 'progress',
        processed: 0,
        total: 2,
        current_document_id: 'aaaaaaaa_one',
        current_title: 'One',
        status: 'started',
      },
      {
        event_type: 'progress',
        processed: 1,
        total: 2,
        current_document_id: 'aaaaaaaa_one',
        current_title: 'One',
        status: 'completed',
        outcome: 'success',
        elapsed_seconds: 1.5,
      },
      {
        event_type: 'summary',
        vault_id: 'v1',
        reabstracted_count: 1,
        skipped_pdf_count: 0,
        failed_count: 0,
        entries: [
          {
            document_id: 'aaaaaaaa_one',
            outcome: 'success',
            error_message: null,
            elapsed_seconds: 1.5,
          },
        ],
      },
    ];

    apiStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockImplementation(async (_stream, onEvent) => {
      for (const event of events) {
        onEvent(event as unknown as Record<string, unknown>);
      }
    });

    const onEvent = vi.fn();
    await startReabstract('v1', onEvent);

    expect(onEvent).toHaveBeenCalledTimes(3);
    expect(onEvent.mock.calls[0][0]).toEqual(events[0]);
    expect(onEvent.mock.calls[1][0]).toEqual(events[1]);
    expect(onEvent.mock.calls[2][0]).toEqual(events[2]);
    expect(onEvent.mock.calls[2][0].event_type).toBe('summary');
  });

  it('A2b: drops events with unknown event_type with a console warning', async () => {
    apiStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockImplementation(async (_stream, onEvent) => {
      onEvent({ event_type: 'unknown', stray: true });
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const onEvent = vi.fn();
    await startReabstract('v1', onEvent);

    expect(onEvent).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it('A3: surfaces 409 reabstract_already_in_flight before any event', async () => {
    const err = new ApiError(
      'reabstract_already_in_flight',
      'A reabstract is already running on this vault.',
      { start_time: '2026-05-21T18:00:00Z' },
    );
    apiStreamMock.mockRejectedValue(err);

    const onEvent = vi.fn();
    await expect(startReabstract('v1', onEvent)).rejects.toBe(err);

    // ApiError preserves the conflict identifier so the panel can render
    // the specific message rather than a generic failure.
    expect(err.code).toBe('reabstract_already_in_flight');
    // Stream was never opened, so onEvent and readSSEStream were untouched.
    expect(onEvent).not.toHaveBeenCalled();
    expect(readSSEStreamMock).not.toHaveBeenCalled();
  });
});

describe('getDeferredCount', () => {
  beforeEach(() => {
    apiPostMock.mockReset();
  });

  it('A4: posts a catalog discover with the right filter and returns total_available (not results.length)', async () => {
    // Mock returns a single result row (length 1) but total_available 73.
    // A correct implementation reads total_available; a wrong one (.length)
    // would return 1. The asymmetry is the anti-coincidental-pass trap.
    apiPostMock.mockResolvedValue({
      mode: 'catalog',
      results: [{ document: { id: 'aaaaaaaa_one', title: 'One' } }],
      total_available: 73,
      cursor: null,
    } as never);

    const count = await getDeferredCount('v1');

    expect(count).toBe(73);
    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const [path, body] = apiPostMock.mock.calls[0];
    expect(path).toBe('/sage_vaults/v1/discover');
    expect(body).toEqual({
      mode: 'catalog',
      filters: { pipeline_status: 'abstraction_skipped' },
      limit: 1,
    });
  });

  it('A4b: returns 0 when total_available is 0 (empty vault path)', async () => {
    apiPostMock.mockResolvedValue({
      mode: 'catalog',
      results: [],
      total_available: 0,
      cursor: null,
    } as never);

    expect(await getDeferredCount('v1')).toBe(0);
  });
});
