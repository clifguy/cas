import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiError } from '../client';
import * as client from '../client';
import {
  sourceTypeForFilename,
  uploadBatchIngest,
  detectIngestProfile,
  PROFILE_PROBE_PATH,
} from '../ingest';
import type { BatchIngestEvent, BatchIngestUploadMetadata } from '../types';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return {
    ...actual,
    apiUploadStream: vi.fn(),
    readSSEStream: vi.fn(),
    apiPost: vi.fn(),
  };
});

const apiUploadStreamMock = vi.mocked(client.apiUploadStream);
const readSSEStreamMock = vi.mocked(client.readSSEStream);
const apiPostMock = vi.mocked(client.apiPost);

// ---------------------------------------------------------------------------
// A. Source-type derivation
// ---------------------------------------------------------------------------

describe('sourceTypeForFilename', () => {
  it('A1: maps known extensions (case-insensitive) to SAGE source types', () => {
    expect(sourceTypeForFilename('notes.md')).toBe('markdown');
    expect(sourceTypeForFilename('notes.markdown')).toBe('markdown');
    expect(sourceTypeForFilename('Brief.DOCX')).toBe('docx');
    expect(sourceTypeForFilename('sheet.xlsx')).toBe('xlsx');
    expect(sourceTypeForFilename('Deck.PPTX')).toBe('pptx');
    expect(sourceTypeForFilename('scan.PDF')).toBe('pdf');
  });

  it('A2: returns null for unknown or extension-less names', () => {
    expect(sourceTypeForFilename('notes.txt')).toBeNull();
    expect(sourceTypeForFilename('README')).toBeNull();
    expect(sourceTypeForFilename('archive.tar.gz')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// B. Multipart upload client
// ---------------------------------------------------------------------------

describe('uploadBatchIngest', () => {
  beforeEach(() => {
    apiUploadStreamMock.mockReset();
    readSSEStreamMock.mockReset();
  });

  it('B1: posts multipart to documents:batch with a position-aligned metadata envelope', async () => {
    apiUploadStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockResolvedValue();

    const items = [
      { file: new File(['a'], 'a.md'), source_type: 'markdown' },
      { file: new File(['b'], 'b.pdf'), source_type: 'pdf' },
    ];
    await uploadBatchIngest('v1', items, vi.fn(), undefined, { inferEdges: true, needsReview: false });

    expect(apiUploadStreamMock).toHaveBeenCalledTimes(1);
    const [path, form] = apiUploadStreamMock.mock.calls[0] as [string, FormData];
    expect(path).toBe('/sage_vaults/v1/documents:batch');

    const fileParts = form.getAll('files') as File[];
    expect(fileParts.map(f => f.name)).toEqual(['a.md', 'b.pdf']);

    const metadata = JSON.parse(form.get('metadata') as string) as BatchIngestUploadMetadata;
    expect(metadata.infer_edges).toBe(true);
    expect(metadata.needs_review).toBe(false);
    expect(metadata.files.map(f => f.source_type)).toEqual(['markdown', 'pdf']);
  });

  it('B2: metadata.files order matches the appended file parts order (alignment guard)', async () => {
    apiUploadStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockResolvedValue();

    // Three distinct source types in a deliberate order. A mis-aligned or
    // re-sorted mapping would surface here even though the counts match.
    const items = [
      { file: new File(['1'], 'first.docx'), source_type: 'docx' },
      { file: new File(['2'], 'second.md'), source_type: 'markdown' },
      { file: new File(['3'], 'third.xlsx'), source_type: 'xlsx' },
    ];
    await uploadBatchIngest('v1', items, vi.fn());

    const [, form] = apiUploadStreamMock.mock.calls[0] as [string, FormData];
    const partNames = (form.getAll('files') as File[]).map(f => f.name);
    const metadata = JSON.parse(form.get('metadata') as string) as BatchIngestUploadMetadata;

    expect(partNames).toEqual(['first.docx', 'second.md', 'third.xlsx']);
    expect(metadata.files.map(f => f.source_type)).toEqual(['docx', 'markdown', 'xlsx']);
  });

  it('B2b: defaults infer_edges=false and needs_review=true when opts omitted', async () => {
    apiUploadStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockResolvedValue();

    await uploadBatchIngest('v1', [{ file: new File(['a'], 'a.md'), source_type: 'markdown' }], vi.fn());

    const [, form] = apiUploadStreamMock.mock.calls[0] as [string, FormData];
    const metadata = JSON.parse(form.get('metadata') as string) as BatchIngestUploadMetadata;
    expect(metadata.infer_edges).toBe(false);
    expect(metadata.needs_review).toBe(true);
  });

  it('B3: forwards each parsed SSE event to onEvent in stream order', async () => {
    const events: BatchIngestEvent[] = [
      { event_type: 'progress', file_index: 0, total_files: 1, filename: 'a.md', stage: 'projection', status: 'started' },
      { event_type: 'progress', file_index: 0, total_files: 1, filename: 'a.md', stage: 'projection', status: 'completed', document_id: 'd1' },
      {
        event_type: 'summary',
        documents_created: { new: 1, new_version: 0 },
        metadata_pending: 1,
        edges_created: {},
        edges_staged: {},
        edges_removed: 0,
        edges_dropped: 0,
        abstracts_generated: 1,
        abstracts_deferred: 0,
        error_count: 0,
        errors: [],
      },
    ];

    apiUploadStreamMock.mockResolvedValue({} as ReadableStream<Uint8Array>);
    readSSEStreamMock.mockImplementation(async (_stream, onEvent) => {
      for (const event of events) {
        onEvent(event as unknown as Record<string, unknown>);
      }
    });

    const onEvent = vi.fn();
    await uploadBatchIngest('v1', [{ file: new File(['a'], 'a.md'), source_type: 'markdown' }], onEvent);

    expect(onEvent).toHaveBeenCalledTimes(3);
    expect(onEvent.mock.calls[0][0]).toEqual(events[0]);
    expect(onEvent.mock.calls[2][0]).toEqual(events[2]);
    expect((onEvent.mock.calls[2][0] as BatchIngestEvent).event_type).toBe('summary');
  });

  it('B4: surfaces a pre-stream ApiError without opening the stream', async () => {
    const err = new ApiError('invalid_batch_metadata', 'files length does not match');
    apiUploadStreamMock.mockRejectedValue(err);

    const onEvent = vi.fn();
    await expect(
      uploadBatchIngest('v1', [{ file: new File(['a'], 'a.md'), source_type: 'markdown' }], onEvent),
    ).rejects.toBe(err);

    expect(onEvent).not.toHaveBeenCalled();
    expect(readSSEStreamMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// D. Profile detection
// ---------------------------------------------------------------------------

describe('detectIngestProfile', () => {
  beforeEach(() => {
    apiPostMock.mockReset();
    window.history.replaceState({}, '', '/ingest');
  });
  afterEach(() => {
    window.history.replaceState({}, '', '/ingest');
  });

  it('D1: probe local_profile_only resolves to hosted', async () => {
    apiPostMock.mockRejectedValue(new ApiError('local_profile_only', 'co-located capability', { status: 501 }));
    expect(await detectIngestProfile('v1')).toBe('hosted');
  });

  it('D2: any non-501 outcome resolves to co-located', async () => {
    apiPostMock.mockRejectedValue(new ApiError('invalid_directory', 'no such directory'));
    expect(await detectIngestProfile('v1')).toBe('co-located');

    apiPostMock.mockReset();
    apiPostMock.mockResolvedValue({ files: [], warnings: [] } as never);
    expect(await detectIngestProfile('v1')).toBe('co-located');
  });

  it('D3: probes the non-existent sentinel path, never a real one', async () => {
    apiPostMock.mockRejectedValue(new ApiError('invalid_directory', 'no such directory'));
    await detectIngestProfile('v1');

    const [path, body] = apiPostMock.mock.calls[0] as [string, { directory: string }];
    expect(path).toBe('/app/scan');
    expect(body.directory).toBe(PROFILE_PROBE_PATH);
    expect(body.directory).not.toBe('');
    expect(body.directory).not.toBe('/');
  });

  it('D4: a ?profile= URL override wins without probing', async () => {
    window.history.replaceState({}, '', '/ingest?profile=hosted');
    expect(await detectIngestProfile('v1')).toBe('hosted');
    expect(apiPostMock).not.toHaveBeenCalled();

    window.history.replaceState({}, '', '/ingest?profile=colocated');
    expect(await detectIngestProfile('v1')).toBe('co-located');
    expect(apiPostMock).not.toHaveBeenCalled();
  });
});
