import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import Search from '../Search';
import type { VaultContext } from '../../App';
import type { VaultSummary, DiscoverResponse, DiscoverHit } from '../../api/types';
import { vi } from 'vitest';

/**
 * Tests for Search view catalog-mode integration, pagination, and sortable tables.
 *
 * Covers:
 *  - TypeScript type: catalog mode accepted in DiscoverRequest
 *  - Dashboard drill-down uses catalog mode (no query)
 *  - Pagination controls appear when total_available > limit
 *  - Browse mode hides query input and uses catalog mode
 *  - Sortable table columns with sort_by/sort_order API params
 *  - Date column displayed in table views
 */

// --- Mock the discover API ---
vi.mock('../../api/discover', () => ({
  discover: vi.fn(),
}));

import { discover } from '../../api/discover';
const mockDiscover = vi.mocked(discover);

// --- Test fixtures ---

const mockVault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: 'A test vault',
  storage_root: '/tmp/test',
  doc_types: [
    { value: 'patent_draft', label: 'Patent Draft' },
    { value: 'reference', label: 'Reference' },
  ],
  lifecycle_states: [
    { value: 'draft', label: 'Draft', is_terminal: false },
    { value: 'active', label: 'Active', is_terminal: false },
  ],
  adapters: [],
  projects: ['pim_health'],
};

function makeHit(id: string, title: string, overrides?: Partial<DiscoverHit>): DiscoverHit {
  return {
    document: {
      id,
      title,
      lifecycle_status: 'active',
      source_type: 'docx',
      source_path: `/vault/${title}.docx`,
      version_label: null,
      project: 'pim_health',
      doc_type: 'patent_draft',
      tags: [],
      document_date: '2026-03-15T00:00:00Z',
      source_modified_at: null,
    },
    chunk_content: null,
    heading_path: null,
    relevance_score: null,
    ...overrides,
  };
}

function makeCatalogResponse(count: number, total: number, offset = 0): DiscoverResponse {
  const results: DiscoverHit[] = [];
  for (let i = 0; i < count; i++) {
    const idx = offset + i + 1;
    results.push(makeHit(`doc-${idx}`, `Document ${idx}`));
  }
  return { mode: 'catalog', results, total_available: total, cursor: null };
}

// --- Test wrapper ---

function TestWrapper({
  vaultId,
  vault,
  initialEntries = ['/search'],
}: {
  vaultId: string;
  vault: VaultSummary | null;
  initialEntries?: string[];
}) {
  const ctx: VaultContext = { vaultId, vault, vaults: vault ? [vault] : [] };
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route element={<WrapperWithContext ctx={ctx} />}>
          <Route path="search" element={<Search />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

function WrapperWithContext({ ctx }: { ctx: VaultContext }) {
  return <Outlet context={ctx} />;
}

// --- Tests ---

beforeEach(() => {
  mockDiscover.mockReset();
});

describe('Search view: dashboard drill-down (catalog mode)', () => {
  it('uses catalog mode (not keyword) for dashboard filter drill-downs', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(2, 2));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?pipeline_status=failed']}
      />,
    );

    // Wait for the API call to complete and results to render
    await screen.findByText('Document 1');

    // Verify discover was called with catalog mode, no query
    expect(mockDiscover).toHaveBeenCalledTimes(1);
    const [vaultId, request] = mockDiscover.mock.calls[0];
    expect(vaultId).toBe('test_vault');
    expect(request.mode).toBe('catalog');
    expect(request).not.toHaveProperty('query');
    expect(request.filters).toEqual({ pipeline_status: 'failed' });
  });

  it('displays total count from total_available', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?doc_type=patent_draft']}
      />,
    );

    // Should show total from total_available, not just results.length
    await screen.findByText(/127 results/);
  });

  it('shows pagination controls when total_available exceeds page size', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?lifecycle_status=active']}
      />,
    );

    await screen.findByText('Document 1');

    // Should have a Next button but no Previous on first page
    expect(screen.getByRole('button', { name: /next/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /previous/i })).not.toBeInTheDocument();
  });

  it('paginates forward with offset and shows Previous button', async () => {
    // Page 1
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127));
    const user = userEvent.setup();

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?lifecycle_status=active']}
      />,
    );

    await screen.findByText('Document 1');

    // Click Next
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127, 50));
    await user.click(screen.getByRole('button', { name: /next/i }));

    await screen.findByText('Document 51');

    // Second call should have offset
    const [, secondRequest] = mockDiscover.mock.calls[1];
    expect(secondRequest.offset).toBe(50);
    expect(secondRequest.mode).toBe('catalog');

    // Previous button should now appear
    expect(screen.getByRole('button', { name: /previous/i })).toBeInTheDocument();
  });

  it('hides Next button on the last page', async () => {
    // 3 results out of 3 total -- single page, no Next
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?pipeline_status=failed']}
      />,
    );

    await screen.findByText('Document 1');

    expect(screen.queryByRole('button', { name: /next/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /previous/i })).not.toBeInTheDocument();
  });
});

describe('Search view: Browse (catalog) mode in main search', () => {
  it('offers Browse as a search mode option', () => {
    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    const modeSelect = screen.getByRole('combobox');
    const options = within(modeSelect).getAllByRole('option');
    const optionValues = options.map(o => (o as HTMLOptionElement).value);
    expect(optionValues).toContain('browse');
  });

  it('hides the query input when Browse mode is selected', async () => {
    const user = userEvent.setup();
    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    // Select Browse mode
    const modeSelect = screen.getByRole('combobox');
    await user.selectOptions(modeSelect, 'browse');

    // Query input should not be present
    expect(screen.queryByPlaceholderText(/search documents/i)).not.toBeInTheDocument();
  });

  it('sends catalog mode request without query when Browse is used', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(10, 10));
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    // Select Browse mode
    const modeSelect = screen.getByRole('combobox');
    await user.selectOptions(modeSelect, 'browse');

    // Click the search/browse button
    await user.click(screen.getByRole('button', { name: /browse/i }));

    expect(mockDiscover).toHaveBeenCalledTimes(1);
    const [, request] = mockDiscover.mock.calls[0];
    expect(request.mode).toBe('catalog');
    expect(request).not.toHaveProperty('query');
  });

  it('shows pagination in Browse mode results', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(20, 85));
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    await user.selectOptions(screen.getByRole('combobox'), 'browse');
    await user.click(screen.getByRole('button', { name: /browse/i }));

    await screen.findByText('Document 1');
    expect(screen.getByRole('button', { name: /next/i })).toBeInTheDocument();
    expect(screen.getByText(/85 results/)).toBeInTheDocument();
  });
});

describe('Search view: existing search modes preserved', () => {
  it('renders Hybrid, Semantic, Keyword modes alongside Browse', () => {
    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    const modeSelect = screen.getByRole('combobox');
    const options = within(modeSelect).getAllByRole('option');
    const labels = options.map(o => o.textContent);
    expect(labels).toEqual(['Hybrid', 'Semantic', 'Keyword', 'Browse']);
  });

  it('shows query input for non-Browse modes', () => {
    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    expect(screen.getByPlaceholderText(/search documents/i)).toBeInTheDocument();
  });

  it('submits semantic search with query as before', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'Test Doc', { relevance_score: 0.92, chunk_content: 'Some content' })],
      total_available: 1,
      cursor: null,
    });
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    await user.type(screen.getByPlaceholderText(/search documents/i), 'architecture');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await screen.findByText('Test Doc');

    const [, request] = mockDiscover.mock.calls[0];
    expect(request.mode).toBe('semantic');
    expect(request.query).toBe('architecture');
    expect(request.use_hybrid).toBe(true);
  });
});

describe('Search view: sortable table in drill-down', () => {
  it('renders a Date column in drill-down results', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(2, 2));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?doc_type=patent_draft']}
      />,
    );

    await screen.findByText('Document 1');

    const table = screen.getByRole('table');
    const headers = within(table).getAllByRole('columnheader');
    const headerTexts = headers.map(h => h.textContent);
    expect(headerTexts).toContain('Date');
  });

  it('renders sortable column headers as buttons in drill-down', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(2, 2));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?doc_type=patent_draft']}
      />,
    );

    await screen.findByText('Document 1');

    const table = screen.getByRole('table');
    const sortButtons = within(table).getAllByRole('button');
    const labels = sortButtons.map(b => b.textContent?.replace(/[^a-zA-Z ]/g, '').trim());
    expect(labels).toContain('Title');
    expect(labels).toContain('Date');
    expect(labels).toContain('Status');
  });

  it('sends sort_by and sort_order when column header is clicked', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?doc_type=patent_draft']}
      />,
    );

    await screen.findByText('Document 1');

    // Click the Title sort button
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const table = screen.getByRole('table');
    const titleSortBtn = within(table).getAllByRole('button').find(b => b.textContent?.includes('Title'));
    expect(titleSortBtn).toBeTruthy();
    await user.click(titleSortBtn!);

    // Should re-fetch with sort params
    const [, sortRequest] = mockDiscover.mock.calls[1];
    expect(sortRequest.sort_by).toBe('title');
    expect(sortRequest.sort_order).toBeDefined();
  });
});

describe('Search view: sortable table in Browse mode', () => {
  it('renders Browse results as a table with Date column', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(5, 5));
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    await user.selectOptions(screen.getByRole('combobox'), 'browse');
    await user.click(screen.getByRole('button', { name: /browse/i }));

    await screen.findByText('Document 1');

    const table = screen.getByRole('table');
    const headers = within(table).getAllByRole('columnheader');
    const headerTexts = headers.map(h => h.textContent?.replace(/[^a-zA-Z ]/g, '').trim());
    expect(headerTexts).toContain('Title');
    expect(headerTexts).toContain('Type');
    expect(headerTexts).toContain('Date');
    expect(headerTexts).toContain('Status');
  });

  it('sends sort params when Browse table header is clicked', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    await user.selectOptions(screen.getByRole('combobox'), 'browse');
    await user.click(screen.getByRole('button', { name: /browse/i }));

    await screen.findByText('Document 1');

    // Click Date sort button
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const table = screen.getByRole('table');
    const dateSortBtn = within(table).getAllByRole('button').find(b => b.textContent?.includes('Date'));
    expect(dateSortBtn).toBeTruthy();
    await user.click(dateSortBtn!);

    const [, sortRequest] = mockDiscover.mock.calls[1];
    expect(sortRequest.sort_by).toBe('document_date');
  });

  it('defaults to lifecycle-first sort on initial Browse', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(5, 5));
    const user = userEvent.setup();

    render(<TestWrapper vaultId="test_vault" vault={mockVault} />);

    await user.selectOptions(screen.getByRole('combobox'), 'browse');
    await user.click(screen.getByRole('button', { name: /browse/i }));

    const [, request] = mockDiscover.mock.calls[0];
    expect(request.sort_by).toBeUndefined();
    expect(request.sort_order).toBeUndefined();
  });
});
