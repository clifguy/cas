import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import { useState } from 'react';

vi.mock('../../api/vaults', () => ({
  getVaultConfig: vi.fn(),
  updateVaultConfig: vi.fn(),
}));

import Settings, {
  IdentityEditor,
  JsonEditor,
  DocTypesEditor,
  LifecycleEditor,
  AbstractionEditor,
} from '../Settings';
import { getVaultConfig, updateVaultConfig } from '../../api/vaults';
import type {
  VaultConfig,
  VaultIdentityConfig,
  DocTypeConfig,
  LifecycleStateConfig,
  LifecycleTransitionConfig,
  VaultAbstractionConfig,
} from '../../api/types';
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

// ---------------------------------------------------------------------------
// Resync-on-prop-change coverage
//
// Each section editor seeds its editable draft from a prop and re-seeds it when
// a fresh prop object arrives (post-save refetch). Existing tests cover the
// INITIAL seed; these cover the RESYNC path, which the parent `Settings` flow
// cannot exercise because its `loading` gate unmounts the editor on every
// refetch (a remount re-seeds via the initial path, not the resync). The
// editors are therefore rendered directly, with the refetched prop delivered
// via `rerender` while the editor stays mounted.
// ---------------------------------------------------------------------------

describe('Settings editors — draft resync when a fresh prop arrives', () => {
  it('IdentityEditor re-seeds the editable draft from a refetched config, discarding stale local edits', async () => {
    const user = userEvent.setup();
    // Harness owns the edit toggle so we can re-enter edit mode after the refetch.
    function IdentityHarness({ config }: { config: VaultIdentityConfig }) {
      const [editing, setEditing] = useState(false);
      return (
        <IdentityEditor
          config={config}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={() => setEditing(false)}
          saving={false}
        />
      );
    }

    const original = makeVaultConfig({ name: 'Original Name' }).vault;
    const { rerender } = render(<IdentityHarness config={original} />);

    // Enter edit mode and make a local change (an in-progress edit).
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    const nameInput = screen.getByDisplayValue('Original Name');
    await user.clear(nameInput);
    await user.type(nameInput, 'Locally Typed Name');
    // Save closes edit mode; in the real flow this is what triggers the refetch.
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The refetch delivers a NEW config object whose name differs from BOTH the
    // original and the locally-typed value (anti-coincidental).
    const refetched = makeVaultConfig({ name: 'Server Canonical Name' }).vault;
    rerender(<IdentityHarness config={refetched} />);

    // Re-enter edit mode: the editable draft must reflect the refetched value.
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    await waitFor(() =>
      expect(screen.getByDisplayValue('Server Canonical Name')).toBeInTheDocument(),
    );
    // Anti-stale guard: the now-superseded local edit must NOT linger in the draft.
    expect(screen.queryByDisplayValue('Locally Typed Name')).not.toBeInTheDocument();
  });

  it('JsonEditor re-seeds the textarea draft from refetched section data, discarding stale local edits', async () => {
    const user = userEvent.setup();
    function JsonHarness({ data }: { data: Record<string, unknown> }) {
      const [editing, setEditing] = useState(false);
      return (
        <JsonEditor
          label="Source Adapters"
          data={data}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={() => setEditing(false)}
          saving={false}
        />
      );
    }

    const { rerender } = render(<JsonHarness data={{ adapter: 'original-value' }} />);

    // Enter edit mode and dirty the textarea (the JsonEditor draft IS its text).
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    const textarea = screen.getByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'locally dirtied draft' } });
    expect(textarea).toHaveValue('locally dirtied draft');

    // The refetch delivers fresh section data; the draft must re-seed from it.
    const refetched = { adapter: 'server-value', extra_key: 42 };
    rerender(<JsonHarness data={refetched} />);

    await waitFor(() =>
      expect(textarea).toHaveValue(JSON.stringify(refetched, null, 2)),
    );
    // Anti-stale guard: the superseded local edit must NOT linger.
    expect(textarea).not.toHaveValue('locally dirtied draft');
  });

  it('DocTypesEditor re-seeds the editable draft from a refetched doc-type set', async () => {
    const user = userEvent.setup();
    function DocTypesHarness({ docTypes }: { docTypes: DocTypeConfig[] }) {
      const [editing, setEditing] = useState(false);
      return (
        <DocTypesEditor
          docTypes={docTypes}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={() => setEditing(false)}
          saving={false}
        />
      );
    }

    const original: DocTypeConfig[] = [{ value: 'stale_type', label: 'Stale', description: '' }];
    const { rerender } = render(<DocTypesHarness docTypes={original} />);

    // Enter edit mode: the controlled inputs render the draft.
    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    expect(screen.getByDisplayValue('stale_type')).toBeInTheDocument();

    // The refetch delivers a different doc-type set while the editor stays mounted.
    const refetched: DocTypeConfig[] = [{ value: 'fresh_type', label: 'Fresh', description: '' }];
    rerender(<DocTypesHarness docTypes={refetched} />);

    await waitFor(() => expect(screen.getByDisplayValue('fresh_type')).toBeInTheDocument());
    // Anti-stale guard: the prior draft value must be gone.
    expect(screen.queryByDisplayValue('stale_type')).not.toBeInTheDocument();
  });

  it('LifecycleEditor re-seeds both states and transitions from a refetched lifecycle', async () => {
    const user = userEvent.setup();
    type Lifecycle = {
      base_states_required: boolean;
      states: LifecycleStateConfig[];
      transitions: LifecycleTransitionConfig[];
    };
    function LifecycleHarness({ lifecycle }: { lifecycle: Lifecycle }) {
      const [editing, setEditing] = useState(false);
      return (
        <LifecycleEditor
          lifecycle={lifecycle}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={() => setEditing(false)}
          saving={false}
        />
      );
    }

    const original: Lifecycle = {
      base_states_required: true,
      states: [{ value: 'stale_state', label: 'Stale State' }],
      transitions: [{ from_state: 'stale_from', action: 'stale_action', to_state: 'stale_to' }],
    };
    const { rerender } = render(<LifecycleHarness lifecycle={original} />);

    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    expect(screen.getByDisplayValue('stale_state')).toBeInTheDocument();
    expect(screen.getByDisplayValue('stale_action')).toBeInTheDocument();

    const refetched: Lifecycle = {
      base_states_required: true,
      states: [{ value: 'fresh_state', label: 'Fresh State' }],
      transitions: [{ from_state: 'fresh_from', action: 'fresh_action', to_state: 'fresh_to' }],
    };
    rerender(<LifecycleHarness lifecycle={refetched} />);

    // Covers BOTH resync setters: setStates (states table) and setTransitions
    // (transitions table). Removing either leaves its half stale.
    await waitFor(() => expect(screen.getByDisplayValue('fresh_state')).toBeInTheDocument());
    expect(screen.getByDisplayValue('fresh_action')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('stale_state')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('stale_action')).not.toBeInTheDocument();
  });

  it('AbstractionEditor re-seeds the editable draft from a refetched config', async () => {
    const user = userEvent.setup();
    function AbstractionHarness({ config }: { config: VaultAbstractionConfig }) {
      const [editing, setEditing] = useState(false);
      return (
        <AbstractionEditor
          config={config}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={() => setEditing(false)}
          saving={false}
        />
      );
    }

    const original: VaultAbstractionConfig = { enabled: false, max_abstract_tokens: 100 };
    const { rerender } = render(<AbstractionHarness config={original} />);

    await user.click(screen.getByRole('button', { name: /^edit$/i }));
    expect(screen.getByRole('spinbutton')).toHaveValue(100);
    expect(screen.getByRole('checkbox')).not.toBeChecked();

    // The refetch delivers a fresh config (both fields changed) while mounted.
    const refetched: VaultAbstractionConfig = { enabled: true, max_abstract_tokens: 500 };
    rerender(<AbstractionHarness config={refetched} />);

    await waitFor(() => expect(screen.getByRole('spinbutton')).toHaveValue(500));
    // Anti-stale guards across both fields.
    expect(screen.getByRole('checkbox')).toBeChecked();
    expect(screen.getByRole('spinbutton')).not.toHaveValue(100);
  });
});
