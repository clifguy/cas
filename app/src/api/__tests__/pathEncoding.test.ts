import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../client';
import { pathSegment } from '../client';
import { bulkSetLifecycle, bulkUpdateMetadata } from '../bulk';
import { discover } from '../discover';
import {
  documentContentUrl,
  getDocument,
  getDocumentDownloadUrl,
  openDocument,
  reabstractDocument,
  updateMetadata,
} from '../documents';
import { createEdge, traverse } from '../graph';
import { uploadBatchIngest } from '../ingest';
import { getDeferredCount, startOptimizeContentStore, startReabstract } from '../maintenance';
import {
  confirmStagingEdge,
  dismissStagingEdge,
  listPendingMetadata,
  listStagingEdges,
} from '../review';
import { getVaultConfig, getVaultStats, updateVaultConfig } from '../vaults';
import type { DiscoverRequest, LinkRequest, TraverseRequest } from '../types';

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client');
  return {
    ...actual,
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    apiPut: vi.fn(),
    apiStream: vi.fn(),
    apiUploadStream: vi.fn(),
    readSSEStream: vi.fn(),
  };
});

// Identifiers carrying every character that would change a URL's structure if
// interpolated raw: a separator, a query and a fragment, beside dot runs.
const VAULT = 'v/1?#';
const DOC = 'a..b/..';
const EDGE = 'e/f?g#h';
const V = 'v%2F1%3F%23';
const D = 'a..b%2F..';
const E = 'e%2Ff%3Fg%23h';

const calledPaths = (): string[] =>
  [
    client.apiGet,
    client.apiPost,
    client.apiPut,
    client.apiStream,
    client.apiUploadStream,
  ].flatMap((fn) => vi.mocked(fn).mock.calls.map((call) => call[0] as string));

describe('pathSegment', () => {
  it.each([
    ['ok_id-1', 'ok_id-1'],
    ['a/b', 'a%2Fb'],
    ['x?y', 'x%3Fy'],
    ['z#w', 'z%23w'],
    ['a..b', 'a..b'],
    ['../x', '..%2Fx'],
  ])('encodes %j as %j', (value, expected) => {
    expect(pathSegment(value)).toBe(expected);
  });
});

describe('API path builders encode every identifier segment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const success = { results: [{ status: 'success', document: {}, edge: {} }] };
    vi.mocked(client.apiGet).mockResolvedValue({ items: [], total_available: 0 });
    vi.mocked(client.apiPost).mockResolvedValue({ ...success, total_available: 0 });
    vi.mocked(client.apiPut).mockResolvedValue({});
    vi.mocked(client.apiStream).mockResolvedValue(new ReadableStream());
    vi.mocked(client.apiUploadStream).mockResolvedValue(new ReadableStream());
    vi.mocked(client.readSSEStream).mockResolvedValue(undefined);
  });

  const cases: [string, () => Promise<unknown>, string][] = [
    ['bulkSetLifecycle', () => bulkSetLifecycle(VAULT, []), `/sage_vaults/${V}/lifecycles`],
    ['bulkUpdateMetadata', () => bulkUpdateMetadata(VAULT, []), `/sage_vaults/${V}/metadata`],
    ['discover', () => discover(VAULT, {} as DiscoverRequest), `/sage_vaults/${V}/discover`],
    ['getDocument', () => getDocument(VAULT, DOC), `/sage_vaults/${V}/documents/${D}`],
    ['updateMetadata', () => updateMetadata(VAULT, DOC, {}), `/sage_vaults/${V}/metadata`],
    ['openDocument', () => openDocument(VAULT, DOC), `/sage_vaults/${V}/documents/${D}/open`],
    [
      'getDocumentDownloadUrl',
      () => getDocumentDownloadUrl(VAULT, DOC),
      `/sage_vaults/${V}/documents/${D}/download-url`,
    ],
    [
      'reabstractDocument',
      () => reabstractDocument(VAULT, DOC),
      `/sage_vaults/${V}/documents/${D}/reabstract`,
    ],
    ['traverse', () => traverse(VAULT, {} as TraverseRequest), `/sage_vaults/${V}/traverse`],
    ['createEdge', () => createEdge(VAULT, {} as LinkRequest), `/sage_vaults/${V}/edges`],
    [
      'uploadBatchIngest',
      () => uploadBatchIngest(VAULT, [], () => {}),
      `/sage_vaults/${V}/documents:batch`,
    ],
    [
      'startReabstract',
      () => startReabstract(VAULT, () => {}),
      `/sage_vaults/${V}/maintenance/reabstract-deferred`,
    ],
    ['getDeferredCount', () => getDeferredCount(VAULT), `/sage_vaults/${V}/discover`],
    [
      'startOptimizeContentStore',
      () => startOptimizeContentStore(VAULT),
      `/sage_vaults/${V}/maintenance/optimize-content-store`,
    ],
    [
      'listPendingMetadata',
      () => listPendingMetadata(VAULT),
      `/sage_vaults/${V}/pending-metadata?limit=`,
    ],
    ['listStagingEdges', () => listStagingEdges(VAULT), `/sage_vaults/${V}/staging-edges`],
    [
      'confirmStagingEdge',
      () => confirmStagingEdge(VAULT, EDGE),
      `/sage_vaults/${V}/staging-edges/${E}/confirm`,
    ],
    [
      'dismissStagingEdge',
      () => dismissStagingEdge(VAULT, EDGE),
      `/sage_vaults/${V}/staging-edges/${E}/dismiss`,
    ],
    ['getVaultStats', () => getVaultStats(VAULT), `/sage_vaults/${V}/stats`],
    ['getVaultConfig', () => getVaultConfig(VAULT), `/sage_vaults/${V}/config`],
    ['updateVaultConfig', () => updateVaultConfig(VAULT, {}), `/sage_vaults/${V}/config`],
  ];

  it.each(cases)('%s', async (_name, call, expected) => {
    await call();
    const paths = calledPaths();
    expect(paths).toHaveLength(1);
    if (expected.endsWith('=')) {
      expect(paths[0].startsWith(expected)).toBe(true);
    } else {
      expect(paths[0]).toBe(expected);
    }
  });

  it('documentContentUrl', () => {
    expect(documentContentUrl(VAULT, DOC)).toBe(`/sage_vaults/${V}/documents/${D}/content`);
  });
});
