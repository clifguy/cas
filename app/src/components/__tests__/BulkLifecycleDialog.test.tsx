import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../../api/vaults', () => ({
  getVaultConfig: vi.fn(),
}));
vi.mock('../../api/bulk', () => ({
  bulkSetLifecycle: vi.fn(),
}));

import { BulkLifecycleDialog } from '../BulkLifecycleDialog';
import { getVaultConfig } from '../../api/vaults';
import { bulkSetLifecycle } from '../../api/bulk';
import type { BulkLifecycleResponse, VaultConfig } from '../../api/types';

function makeVaultConfig(actions: string[]): VaultConfig {
  return {
    vault: { id: 'v', name: 'v', description: null, owner: 'system', storage_root: '', brain_root: '', visibility: 'private', members: null, timezone: 'UTC' },
    document_types: { doc_types: [] },
    lifecycle: {
      base_states_required: true,
      states: [],
      transitions: actions.map((a) => ({ from_state: 'active', action: a, to_state: 'archived' })),
    },
    metadata_extraction: {},
    edge_inference: {},
    abstraction: { enabled: false },
  };
}

function makeBulkResponse(succeeded: string[], failed: { id: string; message: string }[] = []): BulkLifecycleResponse {
  return {
    results: [
      ...succeeded.map((id) => ({ document_id: id, status: 'success' as const, document: null, warnings: null, error: null })),
      ...failed.map((f) => ({
        document_id: f.id,
        status: 'error' as const,
        document: null,
        warnings: null,
        error: { error: 'invalid_lifecycle_transition', message: f.message, detail: {} },
      })),
    ],
    success_count: succeeded.length,
    error_count: failed.length,
    total: succeeded.length + failed.length,
  };
}

function renderDialog(props: Partial<Parameters<typeof BulkLifecycleDialog>[0]> = {}) {
  const defaults = {
    vaultId: 'test_vault',
    selectedIds: ['D1', 'D2', 'D3'],
    onResolved: vi.fn(),
    onClose: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  return { ...merged, user: userEvent.setup(), ...render(<BulkLifecycleDialog {...merged} />) };
}

beforeEach(() => {
  vi.mocked(getVaultConfig).mockReset();
  vi.mocked(bulkSetLifecycle).mockReset();
});

describe('BulkLifecycleDialog', () => {
  it('populates the action dropdown from transitions excluding supersede', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive', 'supersede', 'complete']));
    renderDialog();
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    const select = screen.getByRole('combobox', { name: /action/i }) as HTMLSelectElement;
    const optionValues = Array.from(select.options)
      .map((o) => o.value)
      .filter((v) => v !== '');
    expect(optionValues).toEqual(['archive', 'complete']);
  });

  it('disables the apply button until an action is selected', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive', 'complete']));
    const { user } = renderDialog();
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    const apply = screen.getByRole('button', { name: /^apply$/i });
    expect(apply).toBeDisabled();
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    expect(apply).toBeEnabled();
  });

  it('calls bulkSetLifecycle immediately when selection size ≤ threshold (10)', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    vi.mocked(bulkSetLifecycle).mockResolvedValue(makeBulkResponse(['D1', 'D2', 'D3']));
    const { user } = renderDialog({ selectedIds: ['D1', 'D2', 'D3'] });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    expect(bulkSetLifecycle).toHaveBeenCalledTimes(1);
    expect(bulkSetLifecycle).toHaveBeenCalledWith('test_vault', [
      { document_id: 'D1', action: 'archive' },
      { document_id: 'D2', action: 'archive' },
      { document_id: 'D3', action: 'archive' },
    ]);
  });

  it('shows a confirmation step before applying when selection size > threshold', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    const ids = Array.from({ length: 11 }, (_, i) => `D${i}`);
    const { user } = renderDialog({ selectedIds: ids });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    expect(screen.getByTestId('bulk-lifecycle-confirm')).toHaveTextContent(/apply archive to 11 documents/i);
    expect(bulkSetLifecycle).not.toHaveBeenCalled();
  });

  it("confirmation step's confirm button triggers the API call", async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    vi.mocked(bulkSetLifecycle).mockResolvedValue(makeBulkResponse(Array.from({ length: 11 }, (_, i) => `D${i}`)));
    const ids = Array.from({ length: 11 }, (_, i) => `D${i}`);
    const { user } = renderDialog({ selectedIds: ids });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    await user.click(screen.getByRole('button', { name: /confirm and apply/i }));
    expect(bulkSetLifecycle).toHaveBeenCalledTimes(1);
  });

  it('renders per-item success/failure counts and the failed entry detail', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    vi.mocked(bulkSetLifecycle).mockResolvedValue(
      makeBulkResponse(['A'], [{ id: 'B', message: 'archive not valid from completed' }]),
    );
    const { user } = renderDialog({ selectedIds: ['A', 'B'] });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByTestId('bulk-lifecycle-results-summary')).toHaveTextContent(/1 succeeded, 1 failed/i));
    expect(screen.getByText('B', { selector: 'code' })).toBeInTheDocument();
    expect(screen.getByText(/archive not valid from completed/)).toBeInTheDocument();
  });

  it('fires onResolved with succeeded/failed split when results panel is closed', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    vi.mocked(bulkSetLifecycle).mockResolvedValue(
      makeBulkResponse(['A'], [{ id: 'B', message: 'reason' }]),
    );
    const onResolved = vi.fn();
    const { user } = renderDialog({ selectedIds: ['A', 'B'], onResolved });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    await waitFor(() => screen.getByTestId('bulk-lifecycle-results-summary'));
    await user.click(screen.getByRole('button', { name: /^close$/i }));
    expect(onResolved).toHaveBeenCalledWith({ succeeded: ['A'], failed: ['B'] });
  });

  it('sends items with exactly {document_id, action} fields — no extras', async () => {
    vi.mocked(getVaultConfig).mockResolvedValue(makeVaultConfig(['archive']));
    vi.mocked(bulkSetLifecycle).mockResolvedValue(makeBulkResponse(['D1']));
    const { user } = renderDialog({ selectedIds: ['D1'] });
    await waitFor(() => expect(screen.getByRole('combobox', { name: /action/i })).toBeEnabled());
    await user.selectOptions(screen.getByRole('combobox', { name: /action/i }), 'archive');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    expect(bulkSetLifecycle).toHaveBeenCalledTimes(1);
    const [, items] = vi.mocked(bulkSetLifecycle).mock.calls[0];
    expect(items).toHaveLength(1);
    expect(Object.keys(items[0]).sort()).toEqual(['action', 'document_id']);
  });
});
