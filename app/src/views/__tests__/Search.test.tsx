import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet, useLocation } from 'react-router-dom';
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
    { value: 'design_spec', label: 'Design Spec' },
    { value: 'reference', label: 'Reference' },
  ],
  lifecycle_states: [
    { value: 'draft', label: 'Draft', is_terminal: false },
    { value: 'active', label: 'Active', is_terminal: false },
  ],
  adapters: [],
  projects: ['example_vault'],
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
      project: 'example_vault',
      doc_type: 'design_spec',
      tags: [],
      document_date: '2026-03-15',
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

// Location spy: captures the current URL search string on every render.
// Test assertions read the ref's value after user interactions settle.
function LocationSpy({ locationRef }: { locationRef: { current: string } }) {
  const loc = useLocation();
  locationRef.current = loc.search;
  return null;
}

function TestWrapperWithLocation({
  vaultId,
  vault,
  initialEntries = ['/search'],
  locationRef,
}: {
  vaultId: string;
  vault: VaultSummary | null;
  initialEntries?: string[];
  locationRef: { current: string };
}) {
  const ctx: VaultContext = { vaultId, vault, vaults: vault ? [vault] : [] };
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <LocationSpy locationRef={locationRef} />
      <Routes>
        <Route element={<WrapperWithContext ctx={ctx} />}>
          <Route path="search" element={<Search />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
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
        initialEntries={['/search?doc_type=design_spec']}
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
        initialEntries={['/search?doc_type=design_spec']}
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
        initialEntries={['/search?doc_type=design_spec']}
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
        initialEntries={['/search?doc_type=design_spec']}
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

describe('Search view: URL-driven state persistence', () => {
  it('restores query and mode from URL and auto-runs the search', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'Restored')],
      total_available: 1,
      cursor: null,
    });

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?q=architecture&mode=semantic']}
      />,
    );

    await screen.findByText('Restored');

    // Input should reflect the URL query
    const input = screen.getByPlaceholderText(/search documents/i) as HTMLInputElement;
    expect(input.value).toBe('architecture');

    // Mode select should reflect the URL mode
    const modeSelect = screen.getByRole('combobox') as HTMLSelectElement;
    expect(modeSelect.value).toBe('semantic');

    // API call used the URL state
    expect(mockDiscover).toHaveBeenCalledTimes(1);
    const [, request] = mockDiscover.mock.calls[0];
    expect(request.query).toBe('architecture');
    expect(request.mode).toBe('semantic');
    expect(request.use_hybrid).toBe(false);
  });

  it('restores hybrid mode from URL with use_hybrid=true', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'HybridHit')],
      total_available: 1,
      cursor: null,
    });

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?q=foo&mode=hybrid']}
      />,
    );

    await screen.findByText('HybridHit');

    const [, request] = mockDiscover.mock.calls[0];
    expect(request.mode).toBe('semantic');
    expect(request.use_hybrid).toBe(true);
  });

  it('updates the URL with q and mode when a semantic search is submitted', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'Hit')],
      total_available: 1,
      cursor: null,
    });
    const user = userEvent.setup();
    const locationRef = { current: '' };

    render(
      <TestWrapperWithLocation
        vaultId="test_vault"
        vault={mockVault}
        locationRef={locationRef}
      />,
    );

    await user.selectOptions(screen.getByRole('combobox'), 'semantic');
    await user.type(screen.getByPlaceholderText(/search documents/i), 'architecture');
    await user.click(screen.getByRole('button', { name: /^search$/i }));

    await screen.findByText('Hit');

    expect(locationRef.current).toContain('q=architecture');
    expect(locationRef.current).toContain('mode=semantic');
  });

  it('persists filter selections to the URL on submit', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'Filtered')],
      total_available: 1,
      cursor: null,
    });
    const user = userEvent.setup();
    const locationRef = { current: '' };

    render(
      <TestWrapperWithLocation
        vaultId="test_vault"
        vault={mockVault}
        locationRef={locationRef}
      />,
    );

    await user.click(screen.getByRole('button', { name: /show filters/i }));

    // Select first doc type (design_spec) and first lifecycle (draft)
    const selects = screen.getAllByRole('combobox');
    // selects[0] is mode, selects[1] is doc type, selects[2] is lifecycle, selects[3] is project
    await user.selectOptions(selects[1], 'design_spec');
    await user.selectOptions(selects[2], 'draft');

    await user.type(screen.getByPlaceholderText(/search documents/i), 'hello');
    await user.click(screen.getByRole('button', { name: /^search$/i }));

    await screen.findByText('Filtered');

    expect(locationRef.current).toContain('q=hello');
    expect(locationRef.current).toContain('doc_type=design_spec');
    expect(locationRef.current).toContain('lifecycle_status=draft');

    // The API request should include those filters
    const [, request] = mockDiscover.mock.calls[0];
    expect(request.filters).toMatchObject({ doc_type: 'design_spec', lifecycle_status: 'draft' });
  });

  it('restores filter selects from URL on mount', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('doc-1', 'FilteredRestore')],
      total_available: 1,
      cursor: null,
    });

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?q=foo&mode=semantic&doc_type=design_spec&lifecycle_status=draft']}
      />,
    );

    await screen.findByText('FilteredRestore');

    // Open filters to see the selects
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /show filters/i }));

    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
    // selects[0]=mode, selects[1]=doc type, selects[2]=lifecycle
    expect(selects[1].value).toBe('design_spec');
    expect(selects[2].value).toBe('draft');
  });

  it('writes offset to URL when paginating in Browse mode', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127));
    const user = userEvent.setup();
    const locationRef = { current: '' };

    render(
      <TestWrapperWithLocation
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
        locationRef={locationRef}
      />,
    );

    await screen.findByText('Document 1');

    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127, 50));
    await user.click(screen.getByRole('button', { name: /next/i }));

    await screen.findByText('Document 51');

    expect(locationRef.current).toContain('offset=50');
  });

  it('writes sort params to URL when a column header is clicked in Browse mode', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();
    const locationRef = { current: '' };

    render(
      <TestWrapperWithLocation
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
        locationRef={locationRef}
      />,
    );

    await screen.findByText('Document 1');

    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const table = screen.getByRole('table');
    const dateBtn = within(table).getAllByRole('button').find(b => b.textContent?.includes('Date'));
    await user.click(dateBtn!);

    expect(locationRef.current).toContain('sort_by=document_date');
    expect(locationRef.current).toMatch(/sort_order=(asc|desc)/);
  });

  it('does not render dashboard drill-down when mode param is present', async () => {
    // mode=browse + doc_type=design_spec should render the regular Browse/Search view,
    // not the drill-down heading.
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse&doc_type=design_spec']}
      />,
    );

    await screen.findByText('Document 1');

    // The main Search heading should be visible
    expect(screen.getByRole('heading', { name: /^search$/i })).toBeInTheDocument();

    // The drill-down heading ("Doc Type: ...") should not
    expect(screen.queryByRole('heading', { name: /doc type:/i })).not.toBeInTheDocument();
  });

  it('still renders dashboard drill-down when only filter params are present (no mode)', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(2, 2));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?pipeline_status=failed']}
      />,
    );

    await screen.findByText('Document 1');

    // Drill-down heading appears
    expect(screen.getByRole('heading', { name: /failed ingestions/i })).toBeInTheDocument();
  });
});

describe('Search view: bulk selection model (T-0116)', () => {
  it('initializes with no rows selected and no BulkActionBar visible', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
      />,
    );
    await screen.findByText('Document 1');

    const checkboxes = screen.getAllByRole('checkbox');
    for (const cb of checkboxes) {
      expect((cb as HTMLInputElement).checked).toBe(false);
    }
    expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument();
  });

  it('shows the bulk action bar with count 1 when a row is checked', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
      />,
    );
    await screen.findByText('Document 1');

    const firstRowCheckbox = screen.getByTestId('bulk-row-checkbox-doc-1');
    await user.click(firstRowCheckbox);

    expect(screen.getByTestId('bulk-action-bar')).toBeInTheDocument();
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/1 selected/);
  });

  it('checks every visible row when the header select-all is clicked', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
      />,
    );
    await screen.findByText('Document 1');

    const selectAll = screen.getByTestId('bulk-select-all');
    await user.click(selectAll);

    for (let i = 1; i <= 3; i++) {
      const cb = screen.getByTestId(`bulk-row-checkbox-doc-${i}`) as HTMLInputElement;
      expect(cb.checked).toBe(true);
    }
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/3 selected/);
  });

  it('clears selection when URL query parameters change', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(3, 3));
    const user = userEvent.setup();
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
      />,
    );
    await screen.findByText('Document 1');

    await user.click(screen.getByTestId('bulk-row-checkbox-doc-1'));
    await user.click(screen.getByTestId('bulk-row-checkbox-doc-2'));
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/2 selected/);

    // Switch mode to hybrid + add a query — paramsKey changes, selection should clear.
    mockDiscover.mockResolvedValueOnce({
      mode: 'semantic',
      results: [makeHit('hit-x', 'X')],
      total_available: 1,
      cursor: null,
    });
    const modeSelect = screen.getByRole('combobox') as HTMLSelectElement;
    await user.selectOptions(modeSelect, 'hybrid');
    const searchInput = screen.getByPlaceholderText(/search documents/i);
    await user.type(searchInput, 'something');
    await user.click(screen.getByRole('button', { name: /search/i }));

    await screen.findByText('X');
    expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument();
  });

  it('clears selection when the offset (pagination) changes', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127, 0));
    const user = userEvent.setup();
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse']}
      />,
    );
    await screen.findByText('Document 1');

    await user.click(screen.getByTestId('bulk-row-checkbox-doc-1'));
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/1 selected/);

    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(50, 127, 50));
    await user.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText('Document 51');

    expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument();
  });

  it('renders row checkboxes in card mode without a select-all header', async () => {
    mockDiscover.mockResolvedValueOnce({
      mode: 'hybrid',
      results: [makeHit('h-1', 'A'), makeHit('h-2', 'B'), makeHit('h-3', 'C')],
      total_available: 3,
      cursor: null,
    });
    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=hybrid&q=test']}
      />,
    );
    await screen.findByText('A');

    expect(screen.getByTestId('bulk-row-checkbox-h-1')).toBeInTheDocument();
    expect(screen.getByTestId('bulk-row-checkbox-h-2')).toBeInTheDocument();
    expect(screen.getByTestId('bulk-row-checkbox-h-3')).toBeInTheDocument();
    expect(screen.queryByTestId('bulk-select-all')).not.toBeInTheDocument();
  });
});

describe('Search view: tags URL parameter (T-0130)', () => {
  it('populates filters.tags from ?tags=<single>', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(0, 0));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse&tags=e2e-bulk-fixture']}
      />,
    );

    await vi.waitFor(() => expect(mockDiscover).toHaveBeenCalled());

    const [vaultId, request] = mockDiscover.mock.calls[0];
    expect(vaultId).toBe('test_vault');
    expect(request.mode).toBe('catalog');
    expect(request.filters).toEqual({ tags: ['e2e-bulk-fixture'] });
  });

  it('splits comma-separated ?tags=a,b into an array', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(0, 0));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse&tags=a,b']}
      />,
    );

    await vi.waitFor(() => expect(mockDiscover).toHaveBeenCalled());

    const [, request] = mockDiscover.mock.calls[0];
    expect(request.filters?.tags).toEqual(['a', 'b']);
  });

  it('omits filters.tags when the URL has no ?tags param', async () => {
    mockDiscover.mockResolvedValueOnce(makeCatalogResponse(0, 0));

    render(
      <TestWrapper
        vaultId="test_vault"
        vault={mockVault}
        initialEntries={['/search?mode=browse&doc_type=design_spec']}
      />,
    );

    await vi.waitFor(() => expect(mockDiscover).toHaveBeenCalled());

    const [, request] = mockDiscover.mock.calls[0];
    expect(request.filters).toEqual({ doc_type: 'design_spec' });
    expect(request.filters?.tags).toBeUndefined();
  });
});
