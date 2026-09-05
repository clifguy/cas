import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import Sidebar from '../Sidebar';
import { createVault, getDefaultVaultConfig } from '../../api/vaults';
import type { DefaultVaultConfig, VaultSummary } from '../../api/types';

vi.mock('../../api/vaults', () => ({
  createVault: vi.fn(),
  getDefaultVaultConfig: vi.fn(),
}));

// A scaffold nothing else in the repo could produce. If the component ever
// reassembles a config of its own instead of posting what it was served,
// the assertions below see the real defaults here rather than these values.
function sentinelScaffold(): DefaultVaultConfig {
  return {
    vault: {
      id: 'new_vault',
      name: '',
      owner: '',
      storage_root: '/sentinel-root/new_vault/sources',
      brain_root: '/sentinel-root/new_vault/brain',
      visibility: 'personal',
    },
    document_types: {
      doc_types: [{ value: 'sentinel_type', label: 'Sentinel', description: 'Sentinel.' }],
    },
    lifecycle: {
      base_states_required: true,
      states: [{ value: 'sentinel_state', label: 'Sentinel State' }],
      transitions: [
        { from_state: '(new)', action: 'sentinel_action', to_state: 'sentinel_state' },
      ],
    },
    metadata_extraction: { filename_extraction: { separator: '~' } },
    edge_inference: { tier_assignments: [] },
    abstraction: { enabled: false },
  };
}

const vaultList: VaultSummary[] = [];

function renderSidebar() {
  return render(
    <MemoryRouter>
      <Sidebar
        activeVault=""
        onVaultChange={vi.fn()}
        onVaultCreated={vi.fn()}
        vaultList={vaultList}
      />
    </MemoryRouter>,
  );
}

async function openCreatePanel() {
  renderSidebar();
  await userEvent.click(screen.getByTitle('Create new vault'));
  return screen.getByPlaceholderText('vault_id');
}

async function fillAndSubmit(id: string, name: string, owner: string) {
  await userEvent.type(await openCreatePanel(), id);
  await userEvent.type(screen.getByPlaceholderText('Display Name'), name);
  await userEvent.type(screen.getByPlaceholderText('Owner'), owner);
  await userEvent.click(screen.getByRole('button', { name: 'Create' }));
}

describe('Sidebar create-vault', () => {
  beforeEach(() => {
    vi.mocked(createVault).mockReset();
    vi.mocked(getDefaultVaultConfig).mockReset();
  });

  it('posts exactly the scaffold the server served, with only name and owner filled in', async () => {
    const served = sentinelScaffold();
    vi.mocked(getDefaultVaultConfig).mockResolvedValue(served);
    vi.mocked(createVault).mockResolvedValue({} as VaultSummary);

    await fillAndSubmit('new_vault', 'New Vault', 'someone');

    await waitFor(() => expect(createVault).toHaveBeenCalled());
    expect(getDefaultVaultConfig).toHaveBeenCalledWith('new_vault');
    // Built from the served object, not from a second literal: the point is
    // that the client forwards what it was given, whatever that is.
    expect(vi.mocked(createVault).mock.calls[0][0]).toEqual({
      ...served,
      vault: { ...served.vault, name: 'New Vault', owner: 'someone' },
    });
  });

  it('surfaces the error and creates nothing when the scaffold fetch fails', async () => {
    vi.mocked(getDefaultVaultConfig).mockRejectedValue(new Error('SAGE unavailable'));

    await fillAndSubmit('new_vault', 'New Vault', 'someone');

    // The rendered message proves the handler ran and reached its catch, so
    // the non-call below cannot be explained by the click never landing.
    await waitFor(() => expect(screen.getByText('SAGE unavailable')).toBeInTheDocument());
    expect(createVault).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument();
  });

  it('fetches the scaffold on submit, not when the create panel opens', async () => {
    const idInput = await openCreatePanel();
    await userEvent.type(idInput, 'part');

    // The scaffold is keyed on the vault id, so a fetch before the id is
    // final would serve roots for a half-typed name.
    expect(idInput).toHaveValue('part');
    expect(getDefaultVaultConfig).not.toHaveBeenCalled();
  });
});
