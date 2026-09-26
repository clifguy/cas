import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router';
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
import { ApiError } from '../../api/client';
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
      timezone: 'UTC',
      ...overrides,
    },
    document_types: { doc_types: [] },
    lifecycle: { base_states_required: true, states: [], transitions: [] },
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
          label="Adapter Defaults"
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

  describe('LifecycleEditor doc_type scope', () => {
    type Lifecycle = {
      base_states_required: boolean;
      states: LifecycleStateConfig[];
      transitions: LifecycleTransitionConfig[];
    };
    const scoped: Lifecycle = {
      base_states_required: true,
      states: [
        { value: 'active', label: 'Active' },
        { value: 'blocked', label: 'Blocked', doc_types: ['ticket'] },
      ],
      transitions: [
        { from_state: 'active', action: 'block', to_state: 'blocked', doc_types: ['ticket'] },
        { from_state: 'blocked', action: 'unblock', to_state: 'active', doc_types: ['ticket'] },
        { from_state: 'active', action: 'archive', to_state: 'active' },
      ],
    };

    function Harness({ onSave, startEditing = false }: { onSave: (l: Lifecycle) => void; startEditing?: boolean }) {
      const [editing, setEditing] = useState(startEditing);
      return (
        <LifecycleEditor
          lifecycle={scoped}
          editing={editing}
          onEdit={() => setEditing(true)}
          onCancel={() => setEditing(false)}
          onSave={onSave}
          saving={false}
        />
      );
    }

    // Body rows of the states (0) or transitions (1) table; row 0 is the header.
    const bodyRow = (table: 0 | 1, idx: number) =>
      within(screen.getAllByRole('table')[table]).getAllByRole('row')[idx + 1];

    it("renders each state's and transition's scope", () => {
      render(<Harness onSave={vi.fn()} />);
      expect(within(bodyRow(0, 0)).getByText('all')).toBeInTheDocument();
      expect(within(bodyRow(0, 1)).getByText('ticket')).toBeInTheDocument();
      expect(within(bodyRow(1, 0)).getByText('ticket')).toBeInTheDocument();
      expect(within(bodyRow(1, 2)).getByText('all')).toBeInTheDocument();
    });

    // The untouched `unblock` row must come back exactly as loaded: an editor
    // rewriting every row's scope (null to [], say) would change what the
    // vault means by it, and only this arm would notice.
    it('round-trips an edited scope and leaves untouched entries alone', async () => {
      const onSave = vi.fn();
      const user = userEvent.setup();
      render(<Harness onSave={onSave} startEditing />);

      const activeScope = screen.getByLabelText('Doc types for state 1');
      await user.type(activeScope, 'ticket, adr');
      const blockScope = screen.getByLabelText('Doc types for transition 1');
      await user.clear(blockScope);
      await user.click(screen.getByRole('button', { name: /^save$/i }));

      expect(onSave).toHaveBeenCalledTimes(1);
      const saved: Lifecycle = onSave.mock.calls[0][0];
      expect(saved.states[0].doc_types).toEqual(['ticket', 'adr']);
      expect(saved.states[1].doc_types).toEqual(['ticket']);
      expect(saved.transitions[0].doc_types ?? null).toBeNull();
      expect(saved.transitions[1]).toEqual(scoped.transitions[1]);
      expect(saved.transitions[2]).toEqual(scoped.transitions[2]);
    });

    // A single change event (a paste, an autofill, one keystroke) on a row
    // below the end of the scope-text array, then removing a row above it:
    // the text must stay with its own row. Typing character by character
    // would hide a sparse array, so the edit is one event.
    it('keeps edited scope text on its own row when a row above is removed', () => {
      const three: Lifecycle = {
        base_states_required: true,
        states: [
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B', doc_types: ['adr'] },
          { value: 'c', label: 'C' },
        ],
        transitions: [
          { from_state: 'a', action: 'one', to_state: 'b' },
          { from_state: 'b', action: 'two', to_state: 'c', doc_types: ['adr'] },
          { from_state: 'c', action: 'three', to_state: 'a' },
        ],
      };
      render(
        <LifecycleEditor
          lifecycle={three}
          editing
          onEdit={vi.fn()}
          onCancel={vi.fn()}
          onSave={vi.fn()}
          saving={false}
        />,
      );

      fireEvent.change(screen.getByLabelText('Doc types for state 3'), { target: { value: 'ticket' } });
      fireEvent.change(screen.getByLabelText('Doc types for transition 3'), { target: { value: 'ticket' } });
      const [statesTable, transitionsTable] = screen.getAllByRole('table');
      fireEvent.click(within(statesTable).getAllByRole('button', { name: 'Remove' })[0]);
      fireEvent.click(within(transitionsTable).getAllByRole('button', { name: 'Remove' })[0]);

      expect(screen.getByLabelText('Doc types for state 1')).toHaveValue('adr');
      expect(screen.getByLabelText('Doc types for state 2')).toHaveValue('ticket');
      expect(screen.getByLabelText('Doc types for transition 1')).toHaveValue('adr');
      expect(screen.getByLabelText('Doc types for transition 2')).toHaveValue('ticket');
    });

    it("surfaces the server's refusal message on a lifecycle save", async () => {
      const message =
        "the transition 'active -> block -> blocked' applies to doc_type(s) adr outside the scope of the state 'blocked' (ticket)";
      vi.mocked(getVaultConfig).mockResolvedValue({ ...makeVaultConfig(), lifecycle: scoped });
      vi.mocked(updateVaultConfig).mockRejectedValue(new ApiError('invalid_vault_config', message));
      const { user } = renderSettings();
      await waitFor(() => expect(screen.getByText('TestVault')).toBeInTheDocument());

      await user.click(screen.getByRole('button', { name: /^lifecycle$/i }));
      await user.click(screen.getByRole('button', { name: /^edit$/i }));
      await user.click(screen.getByRole('button', { name: /^save$/i }));

      await waitFor(() => expect(screen.getByText(`Error: ${message}`)).toBeInTheDocument());
    });
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
