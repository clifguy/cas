// Vitest specs for the Dashboard's content-store bloat indicator wiring.
//
// Proves the Dashboard threads lancedb_version_count from the stats payload
// into the BloatIndicator card, rendering a healthy state below the flag
// threshold and a flagged state (with remediation) above it.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router-dom';
import Dashboard from '../Dashboard';
import type { VaultContext } from '../../App';
import type { VaultStats, VaultSummary } from '../../api/types';

vi.mock('../../api/vaults', () => ({
  getVaultStats: vi.fn(),
}));

import { getVaultStats } from '../../api/vaults';
const mockGetVaultStats = vi.mocked(getVaultStats);

const mockVault: VaultSummary = {
  id: 'v1',
  name: 'V1',
  description: null,
  storage_root: '/tmp/v1',
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function makeStats(versionCount: number): VaultStats {
  return {
    total_documents: 1,
    by_lifecycle_status: {},
    by_doc_type: {},
    by_source_type: {},
    total_edges: 0,
    by_edge_type: {},
    staging_edge_count: 0,
    lancedb_size_bytes: 1000,
    lancedb_chunk_count: 5,
    lancedb_version_count: versionCount,
    sqlite_size_bytes: 500,
    last_ingestion_at: null,
    health: {
      pending_metadata_count: 0,
      pending_edge_count: 0,
      deferred_abstract_count: 0,
      failed_ingestion_count: 0,
    },
  };
}

function renderDashboard() {
  const ctx: VaultContext = { vaultId: 'v1', vault: mockVault, vaults: [mockVault] };
  return render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <Routes>
        <Route element={<Outlet context={ctx} />}>
          <Route path="dashboard" element={<Dashboard />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockGetVaultStats.mockReset();
});

describe('Dashboard bloat indicator wiring', () => {
  it('renders a healthy bloat card with no remediation below the flag threshold', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(3));
    renderDashboard();

    const card = await screen.findByTestId('bloat-card');
    expect(card).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });

  it('renders a flagged bloat card with remediation above the flag threshold', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(120));
    renderDashboard();

    const card = await screen.findByTestId('bloat-card');
    expect(card).toHaveAttribute('data-bloat-state', 'red');
    const link = screen.getByRole('link', { name: /optimize/i });
    expect(link).toHaveAttribute('href', '/maintenance');
  });
});
