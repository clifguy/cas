import { render, screen } from '@testing-library/react';
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
vi.mock('../../api/documents', () => ({
  getDocument: vi.fn(),
  openDocument: vi.fn(),
}));
vi.mock('../../api/graph', () => ({
  traverse: vi.fn(),
  createEdge: vi.fn(),
}));
vi.mock('../../api/discover', () => ({
  discover: vi.fn(),
}));

import { getDocument, openDocument } from '../../api/documents';
import { traverse } from '../../api/graph';
import { discover } from '../../api/discover';

const mockGetDocument = vi.mocked(getDocument);
const mockOpenDocument = vi.mocked(openDocument);
const mockTraverse = vi.mocked(traverse);
const mockDiscover = vi.mocked(discover);

// --- Fixtures ---
const mockVault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: null,
  storage_root: '/tmp/test',
  doc_types: [{ value: 'patent_draft', label: 'Patent Draft' }],
  lifecycle_states: [{ value: 'active', label: 'Active', is_terminal: false }],
  adapters: [],
  projects: ['pim_health'],
};

const mockDoc: Document = {
  id: 'doc-42',
  title: 'Test Document',
  source_type: 'docx',
  source_path: '/vault/Test.docx',
  lifecycle_status: 'active',
  version_label: null,
  project: 'pim_health',
  tags: [],
  authority_scope: null,
  doc_type: 'patent_draft',
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
  locationRef.current = `${loc.pathname}${loc.search}`;
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

  it('calls openDocument when the Open button is clicked', async () => {
    mockGetDocument.mockResolvedValue(mockDoc);
    mockTraverse.mockResolvedValue(emptyTraverse);
    mockDiscover.mockResolvedValue(emptyDiscover);
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

    expect(mockOpenDocument).toHaveBeenCalledTimes(1);
    expect(mockOpenDocument).toHaveBeenCalledWith('test_vault', 'doc-42');
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
