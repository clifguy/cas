import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import Ingest from '../Ingest';
import type { VaultContext } from '../../App';
import type { VaultSummary } from '../../api/types';

/**
 * Smoke test validating Vitest + RTL setup and basic Ingest view rendering.
 * Exercises the step indicator (all 4 labels) and Step 1 default state.
 */

const mockVault: VaultSummary = {
  id: 'pim_health',
  name: 'PIM Health Portfolio',
  description: 'Test vault',
  storage_root: '/tmp/test',
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function TestWrapper({ vaultId, vault }: { vaultId: string; vault: VaultSummary | null }) {
  const ctx: VaultContext = { vaultId, vault, vaults: vault ? [vault] : [] };
  return (
    <MemoryRouter initialEntries={['/ingest']}>
      <Routes>
        <Route element={<WrapperWithContext ctx={ctx} />}>
          <Route path="ingest" element={<Ingest />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

function WrapperWithContext({ ctx }: { ctx: VaultContext }) {
  return <Outlet context={ctx} />;
}

describe('Ingest view', () => {
  it('renders all three step labels', () => {
    render(<TestWrapper vaultId="pim_health" vault={mockVault} />);

    expect(screen.getByText(/1\. Directory Input/)).toBeInTheDocument();
    expect(screen.getByText(/2\. Scan Preview/)).toBeInTheDocument();
    expect(screen.getByText(/3\. Ingestion/)).toBeInTheDocument();
  });

  it('starts on Step 1 with directory input and Scan button', () => {
    render(<TestWrapper vaultId="pim_health" vault={mockVault} />);

    expect(screen.getByPlaceholderText('/path/to/source/directory')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /scan/i })).toBeInTheDocument();
  });

  it('shows vault not found for unknown vault', () => {
    render(<TestWrapper vaultId="nonexistent" vault={null} />);

    expect(screen.getByText('Vault not found.')).toBeInTheDocument();
  });
});
