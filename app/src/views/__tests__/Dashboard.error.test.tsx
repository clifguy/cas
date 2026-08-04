// Vitest regression spec for the "cloud 5xx renders a blank view" defect: an
// unhandled 5xx from the HTTP/2 edge (empty statusText + non-JSON body) must
// surface as a visible error in the Dashboard, not a silent blank.
//
// Unlike Dashboard.test.tsx, this file drives the *real* API client end-to-end:
// it stubs globalThis.fetch with the cloud failure shape instead of mocking the
// vaults module, so the errorFromResponse message fallback is exercised for real.
// (The two strategies cannot share a file — Dashboard.test.tsx hoists a file-wide
// vi.mock('../../api/vaults') that would replace getVaultStats.)

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, Outlet } from 'react-router';
import Dashboard from '../Dashboard';
import type { VaultContext } from '../../App';
import type { VaultSummary } from '../../api/types';

const originalFetch = globalThis.fetch;

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

describe('Dashboard cloud 5xx error surfacing', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });
  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('T5: renders a visible error (not a blank) when the stats call 5xxes with an empty statusText and a non-JSON body', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response('Internal Server Error', { status: 500, statusText: '' }),
    );

    renderDashboard();

    // The error state must surface the status-derived message end-to-end.
    const banner = await screen.findByText(/HTTP 500/);
    expect(banner).toHaveTextContent(/error/i);

    // The success render (the vault-identity heading) must be absent — proving the
    // component did not fall through to its blank `if (!stats) return null` path.
    expect(screen.queryByRole('heading', { name: mockVault.name })).toBeNull();
  });
});
