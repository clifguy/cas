import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../../api/bulk', () => ({
  bulkUpdateMetadata: vi.fn(),
}));

import { BulkMetadataDialog } from '../BulkMetadataDialog';
import { bulkUpdateMetadata } from '../../api/bulk';
import type { BulkMetadataResponse } from '../../api/types';

function makeResponse(succeeded: string[], failed: { id: string; message: string }[] = []): BulkMetadataResponse {
  return {
    results: [
      ...succeeded.map((id) => ({ document_id: id, status: 'success' as const, document: null, warnings: null, error: null })),
      ...failed.map((f) => ({
        document_id: f.id,
        status: 'error' as const,
        document: null,
        warnings: null,
        error: { error: 'tag_add_conflict', message: f.message, detail: {} },
      })),
    ],
    success_count: succeeded.length,
    error_count: failed.length,
    total: succeeded.length + failed.length,
  };
}

function renderDialog(props: Partial<Parameters<typeof BulkMetadataDialog>[0]> = {}) {
  const defaults = {
    vaultId: 'test_vault',
    selectedIds: ['D1'],
    onResolved: vi.fn(),
    onClose: vi.fn(),
  };
  const merged = { ...defaults, ...props };
  return { ...merged, user: userEvent.setup(), ...render(<BulkMetadataDialog {...merged} />) };
}

beforeEach(() => {
  vi.mocked(bulkUpdateMetadata).mockReset();
});

describe('BulkMetadataDialog', () => {
  it('renders four ops lanes — Tags add, Tags remove, Tier3 set, Tier3 unset', () => {
    renderDialog();
    expect(screen.getByTestId('lane-tags-add')).toBeInTheDocument();
    expect(screen.getByTestId('lane-tags-remove')).toBeInTheDocument();
    expect(screen.getByTestId('lane-tier3-set')).toBeInTheDocument();
    expect(screen.getByTestId('lane-tier3-unset')).toBeInTheDocument();
  });

  it('disables the apply button when all four lanes are empty', () => {
    renderDialog();
    expect(screen.getByRole('button', { name: /^apply$/i })).toBeDisabled();
  });

  it('sends only tags.add when only that lane is populated', async () => {
    vi.mocked(bulkUpdateMetadata).mockResolvedValue(makeResponse(['D1']));
    const { user } = renderDialog({ selectedIds: ['D1'] });
    const addInput = within(screen.getByTestId('lane-tags-add')).getByRole('textbox');
    await user.type(addInput, 'foo,bar');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    expect(bulkUpdateMetadata).toHaveBeenCalledTimes(1);
    expect(bulkUpdateMetadata).toHaveBeenCalledWith('test_vault', [
      { document_id: 'D1', tags: { add: ['foo', 'bar'] } },
    ]);
  });

  it('renders Tier3 > Set as a key-value pair list with an add-row button', () => {
    renderDialog();
    const setLane = screen.getByTestId('lane-tier3-set');
    expect(within(setLane).getByPlaceholderText(/key/i)).toBeInTheDocument();
    expect(within(setLane).getByPlaceholderText(/value/i)).toBeInTheDocument();
    expect(within(setLane).getByRole('button', { name: /add row/i })).toBeInTheDocument();
  });

  it('does not render a delete control when only one Tier3 > Set row exists', () => {
    renderDialog();
    const setLane = screen.getByTestId('lane-tier3-set');
    expect(within(setLane).queryByRole('button', { name: /remove row/i })).toBeNull();
    expect(within(setLane).getByRole('button', { name: /add row/i })).toBeInTheDocument();
  });

  it('renders a delete control on each row once a second row is added', async () => {
    const { user } = renderDialog();
    const setLane = screen.getByTestId('lane-tier3-set');
    await user.click(within(setLane).getByRole('button', { name: /add row/i }));
    expect(within(setLane).getByRole('button', { name: /remove row 1/i })).toBeInTheDocument();
    expect(within(setLane).getByRole('button', { name: /remove row 2/i })).toBeInTheDocument();
  });

  it('deleting a Tier3 > Set row drops its values from the request body', async () => {
    vi.mocked(bulkUpdateMetadata).mockResolvedValue(makeResponse(['D1']));
    const { user } = renderDialog({ selectedIds: ['D1'] });
    const setLane = screen.getByTestId('lane-tier3-set');

    const keyInputs = () => within(setLane).getAllByPlaceholderText(/key/i);
    const valueInputs = () => within(setLane).getAllByPlaceholderText(/value/i);

    await user.type(keyInputs()[0], 'alpha');
    await user.type(valueInputs()[0], 'A');
    await user.click(within(setLane).getByRole('button', { name: /add row/i }));
    await user.type(keyInputs()[1], 'beta');
    await user.type(valueInputs()[1], 'B');

    await user.click(within(setLane).getByRole('button', { name: /remove row 1/i }));

    expect(within(setLane).queryByDisplayValue('alpha')).toBeNull();
    expect(within(setLane).queryByDisplayValue('A')).toBeNull();
    expect(within(setLane).getByDisplayValue('beta')).toBeInTheDocument();
    expect(within(setLane).getByDisplayValue('B')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^apply$/i }));

    expect(bulkUpdateMetadata).toHaveBeenCalledTimes(1);
    expect(bulkUpdateMetadata).toHaveBeenCalledWith('test_vault', [
      { document_id: 'D1', tier3_metadata: { set: { beta: 'B' } } },
    ]);
  });

  it('deleting a row renumbers the remaining delete controls', async () => {
    const { user } = renderDialog();
    const setLane = screen.getByTestId('lane-tier3-set');
    await user.click(within(setLane).getByRole('button', { name: /add row/i }));
    expect(within(setLane).getByRole('button', { name: /remove row 2/i })).toBeInTheDocument();

    await user.click(within(setLane).getByRole('button', { name: /remove row 1/i }));

    expect(within(setLane).queryByRole('button', { name: /remove row/i })).toBeNull();
    expect(within(setLane).getByPlaceholderText(/key/i)).toBeInTheDocument();
    expect(within(setLane).getByPlaceholderText(/value/i)).toBeInTheDocument();
  });

  it('detects Tier3 set/unset overlap client-side and disables apply', async () => {
    const { user } = renderDialog();
    const setLane = screen.getByTestId('lane-tier3-set');
    await user.type(within(setLane).getByPlaceholderText(/key/i), 'ticket_id');
    await user.type(within(setLane).getByPlaceholderText(/value/i), 'T-0001');
    const unsetLane = screen.getByTestId('lane-tier3-unset');
    await user.type(within(unsetLane).getByRole('textbox'), 'ticket_id');
    expect(screen.getByRole('button', { name: /^apply$/i })).toBeDisabled();
    expect(screen.getByText(/tier3.*disjoint/i)).toBeInTheDocument();
  });

  it('shows confirmation step for selections > 10', async () => {
    const ids = Array.from({ length: 11 }, (_, i) => `D${i}`);
    const { user } = renderDialog({ selectedIds: ids });
    const addInput = within(screen.getByTestId('lane-tags-add')).getByRole('textbox');
    await user.type(addInput, 'bulk-test');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    expect(screen.getByTestId('bulk-metadata-confirm')).toHaveTextContent(/update metadata on 11 documents/i);
    expect(bulkUpdateMetadata).not.toHaveBeenCalled();
  });

  it('renders per-item error envelope after a partial-failure response', async () => {
    vi.mocked(bulkUpdateMetadata).mockResolvedValue(
      makeResponse(['A'], [{ id: 'B', message: "tag 'foo' already present" }]),
    );
    const { user } = renderDialog({ selectedIds: ['A', 'B'] });
    const addInput = within(screen.getByTestId('lane-tags-add')).getByRole('textbox');
    await user.type(addInput, 'foo');
    await user.click(screen.getByRole('button', { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByTestId('bulk-metadata-results-summary')).toHaveTextContent(/1 succeeded, 1 failed/i));
    expect(screen.getByText('B', { selector: 'code' })).toBeInTheDocument();
    expect(screen.getByText(/tag 'foo' already present/)).toBeInTheDocument();
  });
});
