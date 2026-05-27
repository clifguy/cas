import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import Review from '../Review';
import type { VaultContext } from '../../App';
import type {
  Document,
  PendingMetadata,
  StagingEdge,
  UpdateMetadataRequest,
  VaultSummary,
} from '../../api/types';
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
import { updateMetadata } from '../../api/documents';

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
  vi.mocked(updateMetadata).mockReset();
});

function makePendingWithFields(
  id: string,
  title: string,
  extracted_fields: PendingMetadata['extracted_fields'],
): PendingMetadata {
  return { ...makePending(id, title), extracted_fields };
}

function lastUpdateMetadataBody(): UpdateMetadataRequest {
  const calls = vi.mocked(updateMetadata).mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1][2];
}

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

describe('Review view: Confirm-One sends CAS-ADR-028 ops-object metadata (T-0127)', () => {
  it('T1: Confirm-One on a CSV tags string sends tags: { add: [...] } (not bare array)', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePendingWithFields('D1', 'Doc 1', {
        tags: { value: 'alpha, beta, gamma', source: 'content' },
      }),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([]);
    vi.mocked(updateMetadata).mockResolvedValue({} as Document);

    const user = userEvent.setup();
    render(<TestWrapper />);

    await waitFor(() => screen.getByRole('button', { name: /^Confirm$/ }));
    await user.click(screen.getByRole('button', { name: /^Confirm$/ }));

    await waitFor(() => expect(updateMetadata).toHaveBeenCalledTimes(1));

    const [vaultId, docId, body] = vi.mocked(updateMetadata).mock.calls[0];
    expect(vaultId).toBe('test_vault');
    expect(docId).toBe('D1');
    expect(body.tags).toEqual({ add: ['alpha', 'beta', 'gamma'] });
    // Anti-coincidental-pass guards: tags must NOT be a bare array, and the
    // body must NOT carry a top-level array under `tags`.
    expect(Array.isArray(body.tags)).toBe(false);
  });

  it('T2: Confirm-One routes non-Tier-1 extracted fields into tier3_metadata.set', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePendingWithFields('D2', 'Doc Two', {
        title: { value: 'Doc One', source: 'filename' },
        author: { value: 'Roman', source: 'content' },
      }),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([]);
    vi.mocked(updateMetadata).mockResolvedValue({} as Document);

    const user = userEvent.setup();
    render(<TestWrapper />);

    await waitFor(() => screen.getByRole('button', { name: /^Confirm$/ }));
    await user.click(screen.getByRole('button', { name: /^Confirm$/ }));

    await waitFor(() => expect(updateMetadata).toHaveBeenCalledTimes(1));

    const body = lastUpdateMetadataBody();
    expect(body.title).toBe('Doc One');
    expect(body.tier3_metadata).toEqual({ set: { author: 'Roman' } });
    // Anti-coincidental-pass guard: `author` must NOT live on the root of the body.
    expect(body).not.toHaveProperty('author');
  });

  it('T3: Confirm-One omits tags and tier3_metadata when neither has content', async () => {
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePendingWithFields('D3', 'Doc Three', {
        title: { value: 'Doc Two', source: 'filename' },
      }),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([]);
    vi.mocked(updateMetadata).mockResolvedValue({} as Document);

    const user = userEvent.setup();
    render(<TestWrapper />);

    await waitFor(() => screen.getByRole('button', { name: /^Confirm$/ }));
    await user.click(screen.getByRole('button', { name: /^Confirm$/ }));

    await waitFor(() => expect(updateMetadata).toHaveBeenCalledTimes(1));

    const body = lastUpdateMetadataBody();
    expect(body.title).toBe('Doc Two');
    // Backend rejects empty ListFieldPatch / Tier3Patch (no actionable operation).
    // The keys must be absent, not present-with-empty-shape.
    expect(body).not.toHaveProperty('tags');
    expect(body).not.toHaveProperty('tier3_metadata');
  });

  it('T4: user-edited tags string also flows through ListFieldPatch.add', async () => {
    // Baseline has an empty `tags` field so the row renders with an input
    // the user can type into. After the user types, the edits map overlays
    // the baseline and the partition logic must still produce ListFieldPatch.add.
    vi.mocked(listPendingMetadata).mockResolvedValue([
      makePendingWithFields('D4', 'Doc Four', {
        tags: { value: '', source: 'content' },
      }),
    ]);
    vi.mocked(listStagingEdges).mockResolvedValue([]);
    vi.mocked(updateMetadata).mockResolvedValue({} as Document);

    const user = userEvent.setup();
    render(<TestWrapper />);

    await waitFor(() => screen.getByRole('button', { name: /^Confirm$/ }));
    // There's exactly one editable value input in this single-row fixture.
    const valueInput = screen.getByDisplayValue('');
    await user.type(valueInput, 'x, y');
    await user.click(screen.getByRole('button', { name: /^Confirm$/ }));

    await waitFor(() => expect(updateMetadata).toHaveBeenCalledTimes(1));

    const body = lastUpdateMetadataBody();
    expect(body.tags).toEqual({ add: ['x', 'y'] });
    expect(Array.isArray(body.tags)).toBe(false);
  });
});
