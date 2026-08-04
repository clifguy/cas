// Regression spec for the "failed load renders a silent empty graph" defect:
// when the traverse/document-load fails, GraphExplorer must surface a visible
// error instead of falling through to an empty graph with no indication of failure.
//
// vis-network / vis-data are mocked because jsdom has no canvas — and so the
// pre-fix (red) run fails cleanly on "no error banner" rather than on a Network
// constructor throw. The graph API modules are mocked to drive the failure path.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router';

// Constructable stubs: the component uses `new Network(...)` / `new DataSet(...)`,
// so the mocks must be `new`-able (arrow/`vi.fn` impls are not constructors).
vi.mock('vis-network', () => ({
  Network: class {
    on() {}
    destroy() {}
  },
}));
vi.mock('vis-data', () => ({
  // `new DataSet(data)` — a bare class ignores the constructor arg, which is all
  // the stub needs (the mocked Network never reads the resulting DataSet).
  DataSet: class {},
}));
vi.mock('../../api/graph', () => ({ traverse: vi.fn() }));
vi.mock('../../api/documents', () => ({ getDocument: vi.fn() }));

import GraphExplorer from '../GraphExplorer';
import type { VaultContext } from '../../App';
import type { VaultSummary } from '../../api/types';
import { traverse } from '../../api/graph';
import { getDocument } from '../../api/documents';

const mockTraverse = vi.mocked(traverse);
const mockGetDocument = vi.mocked(getDocument);

const mockVault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: null,
  storage_root: '/tmp/test',
  doc_types: [],
  lifecycle_states: [{ value: 'active', label: 'Active', is_terminal: false }],
  adapters: [],
  projects: [],
};

function renderGraphExplorer() {
  const ctx: VaultContext = { vaultId: 'test_vault', vault: mockVault, vaults: [mockVault] };
  return render(
    <MemoryRouter initialEntries={['/documents/doc-1/graph']}>
      <Routes>
        <Route element={<Outlet context={ctx} />}>
          <Route path="documents/:id/graph" element={<GraphExplorer />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockTraverse.mockReset();
  mockGetDocument.mockReset();
});

describe('GraphExplorer error surfacing', () => {
  it('renders a visible error instead of a silent empty graph when the load fails', async () => {
    mockTraverse.mockRejectedValueOnce(new Error('traverse failed'));
    // The doc load is irrelevant once traverse rejects, but resolve it so the
    // Promise.all rejection is the traverse error and nothing goes unhandled.
    mockGetDocument.mockResolvedValue({} as Awaited<ReturnType<typeof getDocument>>);

    renderGraphExplorer();

    // The error must surface, and the loading/graph chrome must be gone.
    await screen.findByText(/traverse failed/i);
    expect(screen.queryByText(/loading graph/i)).toBeNull();
    expect(screen.queryByRole('heading', { name: /graph explorer/i })).toBeNull();
  });
});
