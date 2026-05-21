import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import Review from '../Review';
import type { VaultContext } from '../../App';
import type { PendingMetadata, StagingEdge, VaultSummary } from '../../api/types';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../api/review', () => ({
  listPendingMetadata: vi.fn(),
  listStagingEdges: vi.fn(),
  confirmStagingEdge: vi.fn(),
  dismissStagingEdge: vi.fn(),
}));
vi.mock('../../api/documents', () => ({
  updateMetadata: vi.fn(),
}));

import { listPendingMetadata, listStagingEdges } from '../../api/review';

const mockVault: VaultSummary = {
  id: 'test_vault',
  name: 'Test Vault',
  description: 'A test vault',
  storage_root: '/tmp/test',
  doc_types: [],
  lifecycle_states: [],
  adapters: [],
  projects: [],
};

function makePending(id: string, title: string): PendingMetadata {
  return {
    document: {
      id,
      title,
      lifecycle_status: 'active',
      source_type: 'docx',
      source_path: `/vault/${title}.docx`,
      version_label: null,
      project: null,
      tags: [],
      authority_scope: null,
      doc_type: null,
      source_content_hash: 'sha',
      adapter_version: '0',
      created_by: 'system',
      created_at: '2026-05-21',
      last_modified_by: 'system',
      updated_at: '2026-05-21',
      projected_at: null,
      indexed_at: null,
      source_modified_at: null,
      document_date: null,
      semantic_abstract: null,
      pipeline_status: 'projection_complete',
      pipeline_error: null,
      tier3_metadata: null,
    },
    extracted_fields: {
      title: { value: title, source: 'filename' },
    },
  };
}

function makeStagingEdge(id: string): StagingEdge {
  return {
    id,
    source_id: 'src',
    target_id: 'tgt',
    edge_type: 'references',
    inference_evidence: 'evidence',
    confidence_tier: 1,
    created_at: '2026-05-21',
  };
}

function TestWrapper({
  initialEntries = ['/review'],
}: {
  initialEntries?: string[];
} = {}) {
  const ctx: VaultContext = { vaultId: 'test_vault', vault: mockVault, vaults: [mockVault] };
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route element={<Outlet context={ctx} />}>
          <Route path="review" element={<Review />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.mocked(listPendingMetadata).mockReset();
  vi.mocked(listStagingEdges).mockReset();
});

describe('Review view: metadata-tab selection model (T-0116)', () => {
  it('renders per-document checkboxes and a select-all header', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePending('D1', 'Doc 1'),
      makePending('D2', 'Doc 2'),
      makePending('D3', 'Doc 3'),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([]);

    render(<TestWrapper />);

    await waitFor(() => screen.getByTestId('bulk-row-checkbox-D1'));
    expect(screen.getByTestId('bulk-row-checkbox-D2')).toBeInTheDocument();
    expect(screen.getByTestId('bulk-row-checkbox-D3')).toBeInTheDocument();
    expect(screen.getByTestId('bulk-select-all')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId('bulk-row-checkbox-D1'));
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/1 selected/);
  });

  it('clears selection when the active tab changes', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePending('D1', 'Doc 1'),
      makePending('D2', 'Doc 2'),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([makeStagingEdge('E1')]);

    const user = userEvent.setup();
    render(<TestWrapper />);

    await waitFor(() => screen.getByTestId('bulk-row-checkbox-D1'));
    await user.click(screen.getByTestId('bulk-row-checkbox-D1'));
    await user.click(screen.getByTestId('bulk-row-checkbox-D2'));
    expect(screen.getByTestId('bulk-action-bar-count')).toHaveTextContent(/2 selected/);

    await user.click(screen.getByRole('button', { name: /edge review/i }));
    await waitFor(() => expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: /metadata review/i }));
    await waitFor(() => screen.getByTestId('bulk-row-checkbox-D1'));
    expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument();
  });

  it('does NOT render bulk controls on the edges tab; per-row Confirm/Dismiss survive', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([]);
    vi.mocked(listStagingEdges).mockResolvedValue([makeStagingEdge('E1')]);

    render(<TestWrapper initialEntries={['/review?tab=edges']} />);
    await waitFor(() => screen.getByText(/references/i));

    expect(screen.queryByTestId('bulk-action-bar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bulk-select-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bulk-row-checkbox-E1')).not.toBeInTheDocument();
    // The per-row Confirm/Dismiss buttons remain.
    expect(screen.getAllByRole('button', { name: /^confirm$/i }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole('button', { name: /^dismiss$/i }).length).toBeGreaterThan(0);
  });
});
