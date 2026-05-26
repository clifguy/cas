import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';

vi.mock('../../api/vaults', () => ({
  getVaultConfig: vi.fn(),
  updateVaultConfig: vi.fn(),
}));

import Settings from '../Settings';
import { getVaultConfig, updateVaultConfig } from '../../api/vaults';
import type { VaultConfig, VaultIdentityConfig } from '../../api/types';
import type { VaultContext } from '../../App';

function makeVaultConfig(overrides: Partial<VaultIdentityConfig> = {}): VaultConfig {
  return {
    vault: {
      id: 'example_vault',
      name: 'TestVault',
      description: null,
      owner: 'clif',
      storage_root: '/tmp/storage',
      brain_root: '/tmp/brain',
      visibility: 'personal',
      members: null,
      timezone: 'UTC',
      ...overrides,
    },
    document_types: { doc_types: [] },
    lifecycle: { base_states_required: true, states: [], transitions: [] },
    source_adapters: {},
    metadata_extraction: {},
    edge_inference: {},
    abstraction: { enabled: false },
  };
}

function ContextWrapper({ ctx }: { ctx: VaultContext }) {
  return <Outlet context={ctx} />;
}

function renderSettings(vaultId = 'example_vault') {
  const ctx: VaultContext = { vaultId, vault: null, vaults: [] };
  const utils = render(
    <MemoryRouter initialEntries={['/settings']}>
      <Routes>
        <Route element={<ContextWrapper ctx={ctx} />}>
          <Route path="settings" element={<Settings />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { user: userEvent.setup(), ...utils };
}

beforeEach(() => {
  vi.mocked(getVaultConfig).mockReset();
  vi.mocked(updateVaultConfig).mockReset();
});

describe('Settings view — vault-config flow', () => {
  it('renders the loaded config from getVaultConfig with a content-bearing predicate', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig());
    renderSettings();
    await waitFor(() => expect(screen.getByText('TestVault')).toBeInTheDocument());
    expect(screen.queryByText(/loading configuration/i)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    expect(getVaultConfig).toHaveBeenCalledWith('example_vault');
  });

  it('sends a section-keyed payload to updateVaultConfig on save and renders the success affordance', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig());
    vi.mocked(updateVaultConfig).mockResolvedValue({ status: 'ok', vault_id: 'example_vault', warnings: [] });
    const { user } = renderSettings();
    await waitFor(() => expect(screen.getByText('TestVault')).toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    const nameInput = screen.getByDisplayValue('TestVault');
    await user.clear(nameInput);
    await user.type(nameInput, 'TestVault Renamed');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(updateVaultConfig).toHaveBeenCalledTimes(1));
    expect(updateVaultConfig).toHaveBeenCalledWith(
      'example_vault',
      { vault: expect.objectContaining({ name: 'TestVault Renamed' }) },
    );
    const [, sections] = vi.mocked(updateVaultConfig).mock.calls[0];
    expect(Object.keys(sections)).toEqual(['vault']);
    await waitFor(() => expect(screen.getByText('Configuration saved.')).toBeInTheDocument());
  });

  it('surfaces an error message when getVaultConfig rejects (no hang on loading)', async () => {
    vi.mocked(getVaultConfig).mockRejectedValue(new Error('Vault not found'));
    renderSettings();
    await waitFor(() => expect(screen.getByText(/Error: Vault not found/i)).toBeInTheDocument());
    expect(screen.queryByText(/loading configuration/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Settings' })).not.toBeInTheDocument();
  });
});
