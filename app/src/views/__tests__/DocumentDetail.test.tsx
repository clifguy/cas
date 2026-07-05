import { useEffect } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet, useLocation } from 'react-router-dom';
import DocumentDetail from '../DocumentDetail';
import Search from '../Search';
import type { VaultContext } from '../../App';
import type { VaultSummary, Document, TraverseResponse, DiscoverResponse } from '../../api/types';
import { vi } from 'vitest';

/**
 * Tests for DocumentDetail -- specifically, the "Back to search" control.
 *
 * The back control should return the user to the previous history entry,
 * preserving any URL-encoded search state (query, mode, filters, pagination).
 */

// --- Mocks ---
vi.mock('../../api/documents', async () => {
  const actual = await vi.importActual<typeof import('../../api/documents')>('../../api/documents');
  // Keep the real (pure, no-network) documentContentUrl builder; only the
  // network-touching functions are replaced with spies.
  return {
    ...actual,
    getDocument: vi.fn(),
    openDocument: vi.fn(),
    getDocumentDownloadUrl: vi.fn(),
    reabstractDocument: vi.fn(),
  };
});
vi.mock('../../api/graph', () => ({
  traverse: vi.fn(),
  createEdge: vi.fn(),
}));
vi.mock('../../api/discover', () => ({
  discover: vi.fn(),
}));
vi.mock('../../api/ingest', () => ({
  detectIngestProfile: vi.fn(),
}));

import {
  getDocument,
  openDocument,
  getDocumentDownloadUrl,
  reabstractDocument,
} from '../../api/documents';
import { traverse } from '../../api/graph';
import { discover } from '../../api/discover';
import { detectIngestProfile } from '../../api/ingest';
import { ApiError } from '../../api/client';

const mockGetDocument = vi.mocked(getDocument);
const mockOpenDocument = vi.mocked(openDocument);
const mockGetDownloadUrl = vi.mocked(getDocumentDownloadUrl);
const mockReabstractDocument = vi.mocked(reabstractDocument);
const mockTraverse = vi.mocked(traverse);
const mockDiscover = vi.mocked(discover);
const mockDetectIngestProfile = vi.mocked(detectIngestProfile);

// --- Fixtures ---
const mockVault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: null,
  storage_root: '/tmp/test',
  doc_types: [{ value: 'design_spec', label: 'Design Spec' }],
  lifecycle_states: [{ value: 'active', label: 'Active', is_terminal: false }],
  adapters: [],
  projects: ['example_vault'],
};

const mockDoc: Document = {
  id: 'doc-42',
  title: 'Test Document',
  source_type: 'docx',
  source_path: '/vault/Test.docx',
  lifecycle_status: 'active',
  version_label: null,
  project: 'example_vault',
  tags: [],
  authority_scope: null,
  doc_type: 'design_spec',
  source_content_hash: 'abc',
  adapter_version: '1.0',
  created_by: 'test',
  created_at: '2026-03-15T00:00:00Z',
  last_modified_by: 'test',
  updated_at: '2026-03-15T00:00:00Z',
  projected_at: null,
  indexed_at: null,
  source_modified_at: null,
  document_date: null,
  semantic_abstract: null,
  pipeline_status: 'indexed',
  pipeline_error: null,
  tier3_metadata: null,
};

const emptyTraverse: TraverseResponse = { start_id: 'doc-42', nodes: [] };

const emptyDiscover: DiscoverResponse = {
  mode: 'semantic',
  results: [],
  total_available: 0,
  cursor: null,
};

// Location spy
function LocationSpy({ locationRef }: { locationRef: { current: string } }) {
  const loc = useLocation();
  useEffect(() => {
    locationRef.current = `${loc.pathname}${loc.search}`;
  }, [loc.pathname, loc.search, locationRef]);
  return null;
}

function TestAppWithHistory({
  initialEntries,
  initialIndex,
  locationRef,
}: {
  initialEntries: string[];
  initialIndex: number;
  locationRef: { current: string };
}) {
  const ctx: VaultContext = { vaultId: 'test_vault', vault: mockVault, vaults: [mockVault] };
  return (
    <MemoryRouter initialEntries={initialEntries} initialIndex={initialIndex}>
      <LocationSpy locationRef={locationRef} />
      <Routes>
        <Route element={<Outlet context={ctx} />}>
          <Route path="search" element={<Search />} />
          <Route path="documents/:id" element={<DocumentDetail />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  mockGetDocument.mockReset();
  mockOpenDocument.mockReset();
  mockReabstractDocument.mockReset();
  mockTraverse.mockReset();
  mockDiscover.mockReset();
});

describe('DocumentDetail: Back to search', () => {
  it('returns to the previous history entry, preserving search URL params', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);

    const locationRef = { current: '' };
    const user = userEvent.setup();

    render(
      <TestAppWithHistory
        initialEntries={['/search?q=hello&mode=semantic', '/documents/doc-42']}
        initialIndex={1}
        locationRef={locationRef}
      />,
    );

    // Wait for the document to render
    await screen.findByRole('heading', { name: mockDoc.title });

    // Click "Back to search"
    const backControl = screen.getByRole('button', { name: /back to search/i });
    await user.click(backControl);

    // Should land on the previous search URL
    expect(locationRef.current).toBe('/search?q=hello&mode=semantic');
  });

  it('opens via the OS opener under the co-located profile', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
    mockDetectIngestProfile.mockResolvedValue('co-located');
    mockOpenDocument.mockResolvedValue({ opened: true, path: '/vault/Test.docx' });

    const locationRef = { current: '' };
    const user = userEvent.setup();

    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={locationRef}
      />,
    );

    await screen.findByRole('heading', { name: mockDoc.title });

    const openBtn = screen.getByRole('button', { name: /^open$/i });
    await user.click(openBtn);

    await waitFor(() => expect(mockOpenDocument).toHaveBeenCalledWith('test_vault', 'doc-42'));
    expect(mockOpenDocument).toHaveBeenCalledTimes(1);
    expect(mockGetDownloadUrl).not.toHaveBeenCalled();
  });

  it('delivers to the browser via a download URL under the hosted profile', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
    mockDetectIngestProfile.mockResolvedValue('hosted');
    mockGetDownloadUrl.mockResolvedValue({ download_url: 'https://sp.example/dl?t=abc' });
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );
    await screen.findByRole('heading', { name: mockDoc.title });

    await user.click(screen.getByRole('button', { name: /^open$/i }));

    await waitFor(() => expect(mockGetDownloadUrl).toHaveBeenCalledWith('test_vault', 'doc-42'));
    expect(openSpy).toHaveBeenCalledWith('https://sp.example/dl?t=abc', '_blank', 'noopener');
    expect(mockOpenDocument).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('surfaces an error and does not open a window when the download URL fails', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
    mockDetectIngestProfile.mockResolvedValue('hosted');
    mockGetDownloadUrl.mockRejectedValue(new Error('download_url_unavailable: no URL'));
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );
    await screen.findByRole('heading', { name: mockDoc.title });

    await user.click(screen.getByRole('button', { name: /^open$/i }));

    await waitFor(() => expect(screen.getByText(/download_url_unavailable/)).toBeInTheDocument());
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('falls back to the streaming content route when the download URL is unavailable (501)', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
    mockDetectIngestProfile.mockResolvedValue('hosted');
    // The filesystem binding cannot presign: download-url answers 501 and the
    // client surfaces it as an ApiError whose code is the 501 body's code.
    mockGetDownloadUrl.mockRejectedValue(
      new ApiError(
        'download_url_unavailable',
        'The active vault-source binding cannot issue a download URL for document doc-42.',
      ),
    );
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );
    await screen.findByRole('heading', { name: mockDoc.title });

    await user.click(screen.getByRole('button', { name: /^open$/i }));

    await waitFor(() => expect(mockGetDownloadUrl).toHaveBeenCalledWith('test_vault', 'doc-42'));
    expect(openSpy).toHaveBeenCalledWith(
      '/sage_vaults/test_vault/documents/doc-42/content',
      '_blank',
      'noopener',
    );
    await waitFor(() => expect(screen.getByText(/Opened in browser/)).toBeInTheDocument());
    openSpy.mockRestore();
  });

  it('does not fall back to the content route when the download URL fails with a non-501 error', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
    mockDetectIngestProfile.mockResolvedValue('hosted');
    // A different error code must NOT trigger the content-route fallback: the
    // fallback keys on download_url_unavailable specifically, not any failure.
    mockGetDownloadUrl.mockRejectedValue(new ApiError('HTTP_500', 'boom'));
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );
    await screen.findByRole('heading', { name: mockDoc.title });

    await user.click(screen.getByRole('button', { name: /^open$/i }));

    await waitFor(() => expect(screen.getByText(/boom/)).toBeInTheDocument());
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('renders the back control as a button (so it can trigger history.back), not a fixed link', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);

    const locationRef = { current: '' };

    render(
      <TestAppWithHistory
        initialEntries={['/search?q=x&mode=hybrid', '/documents/doc-42']}
        initialIndex={1}
        locationRef={locationRef}
      />,
    );

    await screen.findByRole('heading', { name: mockDoc.title });

    // Back control is a button (so onClick can call navigate(-1)),
    // not a Link to a hardcoded "/search" path.
    const backBtn = screen.getByRole('button', { name: /back to search/i });
    expect(backBtn.tagName).toBe('BUTTON');
  });
});

describe('DocumentDetail: Regenerate abstract', () => {
  it('renders a Regenerate abstract control on the document detail view', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);

    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );

    await screen.findByRole('heading', { name: mockDoc.title });
    expect(
      screen.getByRole('button', { name: /regenerate abstract/i }),
    ).toBeInTheDocument();
  });

  it('kicks off re-abstraction and refreshes the displayed abstract on completion', async () => {
    const refreshed: Document = {
      ...mockDoc,
      pipeline_status: 'abstraction_complete',
      semantic_abstract: 'Freshly regenerated abstract.',
    };
    // First getDocument (initial mount load) returns the stale doc; the poll
    // after re-abstraction returns the refreshed doc so the loop settles at
    // once (no in-progress state to wait through).
    mockGetDocument.mockResolvedValueOnce(mockDoc).mockResolvedValue(refreshed);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockReabstractDocument.mockResolvedValue({
      status: 'reabstract_started',
      document_id: 'doc-42',
      dispatched_at: '2026-07-04T00:00:00Z',
    });

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );

    await screen.findByRole('heading', { name: mockDoc.title });
    // Stale state: mockDoc.semantic_abstract is null -> empty-state copy.
    expect(screen.getByText(/no abstract generated yet/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /regenerate abstract/i }));

    await waitFor(() =>
      expect(mockReabstractDocument).toHaveBeenCalledWith('test_vault', 'doc-42'),
    );
    expect(mockReabstractDocument).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByText('Freshly regenerated abstract.')).toBeInTheDocument(),
    );
  });

  it('surfaces a non-fatal message when a regeneration is already in flight', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockReabstractDocument.mockRejectedValue(
      new ApiError('reabstract_document_already_in_flight', 'already running'),
    );

    const user = userEvent.setup();
    render(
      <TestAppWithHistory
        initialEntries={['/documents/doc-42']}
        initialIndex={0}
        locationRef={{ current: '' }}
      />,
    );

    await screen.findByRole('heading', { name: mockDoc.title });
    await user.click(screen.getByRole('button', { name: /regenerate abstract/i }));

    await waitFor(() =>
      expect(screen.getByText(/already running for this document/i)).toBeInTheDocument(),
    );
    // No crash: the document heading is still present.
    expect(screen.getByRole('heading', { name: mockDoc.title })).toBeInTheDocument();
  });
});
