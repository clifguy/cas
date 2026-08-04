// Vitest specs for the Dashboard's content-store bloat + last-optimize wiring.
//
// Proves the Dashboard threads content_store_version_count (dead tuples),
// content_store_chunk_count (live rows), and content_store_small_fragment_count
// (free pages) from the stats payload into the BloatIndicator card (ok below the
// autovacuum-anchored dead-tuple threshold, flagged above it, and unmoved by
// free space alone), and renders the last-optimize summary card from
// stats.last_optimize.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router';
import Dashboard from '../Dashboard';
import type { VaultContext } from '../../App';
import type { LastOptimizeSummary, VaultStats, VaultSummary } from '../../api/types';

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

function makeStats(
  deadTuples: number,
  liveChunks: number,
  freePages = 0,
  lastOptimize: LastOptimizeSummary | null = null,
): VaultStats {
  return {
    total_documents: 1,
    by_lifecycle_status: {},
    by_doc_type: {},
    by_source_type: {},
    total_edges: 0,
    by_edge_type: {},
    staging_edge_count: 0,
    graph_store_size_bytes: 800,
    content_store_size_bytes: 1000,
    content_store_chunk_count: liveChunks,
    content_store_version_count: deadTuples,
    content_store_small_fragment_count: freePages,
    last_ingestion_at: null,
    last_optimize: lastOptimize,
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
  it('renders a healthy live-vault snapshot with no remediation', async () => {
    // 50 dead / 8,469 live = 0.6% — the case the old absolute thresholds red-flagged.
    mockGetVaultStats.mockResolvedValue(makeStats(50, 8469, 161));
    renderDashboard();

    const card = await screen.findByTestId('bloat-card');
    expect(card).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });

  it('renders a flagged bloat card with remediation above the dead-tuple threshold', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(40, 60, 0)); // 40% dead
    renderDashboard();

    const card = await screen.findByTestId('bloat-card');
    expect(card).toHaveAttribute('data-bloat-state', 'red');
    const link = screen.getByRole('link', { name: /optimize/i });
    expect(link).toHaveAttribute('href', '/maintenance');
  });

  it('does not flag on free space alone when the dead-tuple ratio is healthy', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(1, 999, 100_000)); // 0.1% dead, big free space
    renderDashboard();

    const card = await screen.findByTestId('bloat-card');
    expect(card).toHaveAttribute('data-bloat-state', 'ok');
    expect(screen.queryByRole('link', { name: /optimize/i })).toBeNull();
  });
});

describe('Dashboard last-optimize card', () => {
  it('renders the humanized reclaimed bytes when a last optimize exists', async () => {
    mockGetVaultStats.mockResolvedValue(
      makeStats(3, 100, 0, {
        at: '2026-06-01T12:00:00Z',
        bytes_reclaimed: 169_379_435,
        versions_cleaned: 94,
        fragments_merged: 30,
      }),
    );
    renderDashboard();

    const card = await screen.findByTestId('last-optimize-card');
    expect(card).toHaveTextContent('169.4 MB');
    expect(card).toHaveTextContent(/last optimized/i);
  });

  it('renders a never-optimized affordance when last_optimize is null', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(3, 100, 0, null));
    renderDashboard();

    const card = await screen.findByTestId('last-optimize-card');
    expect(card).toHaveTextContent(/never/i);
  });
});

describe('Dashboard storage stats', () => {
  it('renders the backend-neutral graph-store size and omits the retired SQLite stat', async () => {
    mockGetVaultStats.mockResolvedValue(makeStats(3, 100));
    renderDashboard();

    await screen.findByText('Graph Store');
    expect(screen.getByText('800 B')).toBeInTheDocument();
    expect(screen.getByText('Content Store')).toBeInTheDocument();
    // sqlite_size_bytes is retired; its card must not render.
    expect(screen.queryByText('SQLite')).not.toBeInTheDocument();
  });
});
