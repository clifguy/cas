// Vitest specs for the Dashboard's content-store bloat + last-optimize wiring.
//
// Proves the Dashboard threads content_store_version_count (dead tuples),
// content_store_row_count (live rows across every content-store surface), and
// content_store_small_fragment_count
// (free pages) from the stats payload into the BloatIndicator card (ok below the
// autovacuum-anchored dead-tuple threshold, flagged above it, and unmoved by
// free space alone), and renders the last-optimize summary card from
// stats.last_optimize.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router';
import Dashboard from '../Dashboard';
import type { VaultContext } from '../../App';
import type {
  HealthIndicators,
  LastOptimizeSummary,
  VaultStats,
  VaultSummary,
} from '../../api/types';

vi.mock('../../api/vaults', () => ({
  getVaultStats: vi.fn(),
}));

import { getVaultStats } from '../../api/vaults';
const mockGetVaultStats = vi.mocked(getVaultStats);

const mockVault: VaultSummary = {
  id: 'v1',
  name: 'V1',
  description: null,
  document_count: 0,
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function makeStats(
  deadTuples: number,
  liveRows: number,
  freePages = 0,
  lastOptimize: LastOptimizeSummary | null = null,
  health: Partial<HealthIndicators> = {},
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
    content_store_row_count: liveRows,
    content_store_version_count: deadTuples,
    content_store_small_fragment_count: freePages,
    last_ingestion_at: null,
    last_optimize: lastOptimize,
    health: {
      pending_metadata_count: 0,
      pending_edge_count: 0,
      deferred_abstract_count: 0,
      failed_ingestion_count: 0,
      interrupted_abstract_count: 0,
      ...health,
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

describe('Dashboard health-card drill-down links', () => {
  // The counters exclude documents in a terminal lifecycle state; the
  // links they sit on did not, so a card reading 0 could open a list of
  // 1. Asserting the href alone would pass against a card wired to the
  // wrong counter, so each case reads the rendered count in the same
  // breath as the link it opens.
  const cards = [
    ['Deferred abstracts', 'abstraction_skipped', 'deferred_abstract_count'],
    ['Failed ingestions', 'failed', 'failed_ingestion_count'],
    ['Interrupted abstracts', 'abstraction_interrupted', 'interrupted_abstract_count'],
  ] as const;

  it.each(cards)(
    'the %s card opens a list constrained to the population it counted',
    async (label, pipelineStatus, counter) => {
      mockGetVaultStats.mockResolvedValue(makeStats(3, 100, 0, null, { [counter]: 7 }));
      renderDashboard();

      const link = await screen.findByRole('link', { name: new RegExp(label, 'i') });
      const href = link.getAttribute('href') ?? '';
      const params = new URLSearchParams(href.slice(href.indexOf('?')));

      expect(href.startsWith('/search?')).toBe(true);
      expect(params.get('pipeline_status')).toBe(pipelineStatus);
      expect(params.get('exclude_terminal_lifecycle')).toBe('1');
      expect(link).toHaveTextContent('7');
    },
  );

  it('leaves the deferred card unlinked when abstracts are disabled', async () => {
    // The null-count branch renders a plain div rather than a Link. It
    // is the one health card that must not gain a drill-down, and a
    // refactor that routes every card through one link builder is
    // exactly what would quietly give it one.
    mockGetVaultStats.mockResolvedValue(
      makeStats(3, 100, 0, null, { deferred_abstract_count: null as unknown as number }),
    );
    renderDashboard();

    await screen.findByText('Abstracts disabled');
    expect(screen.queryByRole('link', { name: /Deferred abstracts/i })).toBeNull();
  });
});
